"""Django-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from .parse import ParsedFile, ParsedRelation, ParsedSymbol

MODEL_BASES = {'models.Model', 'django.db.models.Model', 'Model'}
SERIALIZER_BASES = {
    'serializers.ModelSerializer',
    'serializers.Serializer',
    'rest_framework.serializers.ModelSerializer',
    'rest_framework.serializers.Serializer',
}
VIEWSET_BASES = {
    'viewsets.ModelViewSet',
    'viewsets.ViewSet',
    'rest_framework.viewsets.ModelViewSet',
    'rest_framework.viewsets.ViewSet',
}
FIELD_SUFFIXES = {
    'Field',
    'AutoField',
    'BigAutoField',
    'BooleanField',
    'CharField',
    'DateField',
    'DateTimeField',
    'DecimalField',
    'EmailField',
    'FileField',
    'FloatField',
    'ForeignKey',
    'IntegerField',
    'ManyToManyField',
    'OneToOneField',
    'TextField',
    'UUIDField',
}


@dataclass(frozen=True)
class _ClassInfo:
    path: str
    module_name: str
    qualified_name: str
    imports: dict[str, str]
    symbol: ParsedSymbol
    node: ast.ClassDef


@dataclass(frozen=True)
class _URLPattern:
    path: str
    module_name: str
    qualified_name: str
    route: str
    target: str | None
    name: str | None
    kind: str
    line: int
    node: ast.AST


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with Django URL, model, signal, and settings nodes."""
    class_index = _collect_classes(parsed_files)
    model_keys = _detect_class_keys(class_index, MODEL_BASES)
    serializer_keys = _detect_class_keys(class_index, SERIALIZER_BASES)
    viewset_keys = _detect_class_keys(class_index, VIEWSET_BASES)
    direct_urls = _collect_url_patterns(parsed_files)
    all_urls = [*direct_urls, *_included_url_patterns(direct_urls)]

    for parsed in sorted(parsed_files, key=lambda item: item.path):
        if parsed.tree is None:
            continue
        symbols: list[ParsedSymbol] = []
        relations: list[ParsedRelation] = []
        for pattern in sorted([item for item in all_urls if item.path == parsed.path], key=lambda item: (item.line, item.qualified_name)):
            symbols.append(_url_symbol(pattern))
            if pattern.target:
                relations.append(ParsedRelation(pattern.path, pattern.qualified_name, 'URL_HANDLES', pattern.target, pattern.line))
            if pattern.kind == 'include' and pattern.target:
                relations.append(ParsedRelation(pattern.path, pattern.qualified_name, 'INCLUDES_URLCONF', pattern.target, pattern.line))

        symbols.extend(_middleware_symbols(parsed))
        signal_symbols, signal_relations = _signal_symbols(parsed)
        symbols.extend(signal_symbols)
        relations.extend(signal_relations)

        for key in sorted(model_keys, key=lambda item: item[1]):
            if key[0] != parsed.path:
                continue
            model_symbols, model_relations = _model_symbols(class_index[key], class_index, model_keys)
            symbols.extend(model_symbols)
            relations.extend(model_relations)
        for key in sorted(serializer_keys, key=lambda item: item[1]):
            if key[0] != parsed.path:
                continue
            symbol, serializer_relations = _serializer_symbol(class_index[key])
            symbols.append(symbol)
            relations.extend(serializer_relations)
        for key in sorted(viewset_keys, key=lambda item: item[1]):
            if key[0] != parsed.path:
                continue
            symbols.append(_viewset_symbol(class_index[key]))

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


def _detect_class_keys(
    class_index: dict[tuple[str, str], _ClassInfo],
    base_targets: set[str],
) -> set[tuple[str, str]]:
    reference_index = _reference_index(class_index)
    matched: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for key, info in sorted(class_index.items(), key=lambda item: item[0]):
            if key in matched:
                continue
            bases = _resolved_base_targets(info, reference_index)
            if any(base in base_targets or base.rsplit('.', 1)[-1] in {item.rsplit('.', 1)[-1] for item in base_targets} for base in bases):
                matched.add(key)
                changed = True
                continue
            if any(reference_index.get(base) in matched for base in bases):
                matched.add(key)
                changed = True
    return matched


def _collect_url_patterns(parsed_files: list[ParsedFile]) -> list[_URLPattern]:
    patterns: list[_URLPattern] = []
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        for statement in parsed.tree.body:
            if not _assigns_name(statement, 'urlpatterns'):
                continue
            value = statement.value if isinstance(statement, ast.Assign) else None
            for index, call in enumerate(_url_calls(value)):
                pattern = _url_pattern(parsed, call, index)
                if pattern is not None:
                    patterns.append(pattern)
    return sorted(patterns, key=lambda item: (item.path, item.qualified_name))


def _included_url_patterns(patterns: list[_URLPattern]) -> list[_URLPattern]:
    by_module: dict[str, list[_URLPattern]] = {}
    for pattern in patterns:
        by_module.setdefault(pattern.module_name, []).append(pattern)
    included: list[_URLPattern] = []
    for pattern in patterns:
        if pattern.kind != 'include' or pattern.target is None:
            continue
        for child in by_module.get(pattern.target, []):
            if child.kind == 'include':
                continue
            route = _join_routes(pattern.route, child.route)
            qualified_name = f'{pattern.qualified_name}.__include__.{_safe_key(child.qualified_name)}'
            included.append(
                _URLPattern(
                    path=pattern.path,
                    module_name=pattern.module_name,
                    qualified_name=qualified_name,
                    route=route,
                    target=child.target,
                    name=child.name,
                    kind='included_path',
                    line=pattern.line,
                    node=pattern.node,
                )
            )
    return sorted(included, key=lambda item: (item.path, item.qualified_name))


def _url_pattern(parsed: ParsedFile, call: ast.Call, index: int) -> _URLPattern | None:
    call_name = _expr_name(call.func) or ''
    function_name = call_name.rsplit('.', 1)[-1]
    if function_name not in {'path', 're_path'}:
        return None
    route = _literal_arg(call, 0)
    if route is None:
        return None
    raw_target = call.args[1] if len(call.args) > 1 else None
    include_target = _include_target(raw_target, parsed.imports)
    kind = 'include' if include_target is not None else function_name
    target = include_target or (_expr_name(raw_target) if raw_target is not None else None)
    if target is not None:
        target = _resolve_name(target, parsed.module_name, parsed.imports)
    name = _keyword_value(call, 'name')
    qualified_name = f'{parsed.module_name}.__django_url__.{index}.{_safe_key(route)}'
    return _URLPattern(parsed.path, parsed.module_name, qualified_name, route, target, name, kind, call.lineno, call)


def _url_calls(node: ast.AST | None) -> list[ast.Call]:
    if node is None:
        return []
    values = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    return [value for value in values if isinstance(value, ast.Call)]


def _url_symbol(pattern: _URLPattern) -> ParsedSymbol:
    signature = f'{pattern.kind} {pattern.route}; target={pattern.target or "-"}; name={pattern.name or "-"}'
    return ParsedSymbol(
        kind='DjangoURLPattern',
        path=pattern.path,
        qualified_name=pattern.qualified_name,
        module_name=pattern.module_name,
        signature=signature,
        location=_location(pattern.node),
        content_hash=_node_hash(pattern.node),
        owner_qualified_name=pattern.module_name,
    )


def _model_symbols(
    info: _ClassInfo,
    class_index: dict[tuple[str, str], _ClassInfo],
    model_keys: set[tuple[str, str]],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    reference_index = _reference_index(class_index)
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    for target_name in _inheritance_targets(info, reference_index, model_keys, MODEL_BASES):
        relations.append(ParsedRelation(info.path, info.qualified_name, 'INHERITS', target_name, info.node.lineno))
    for statement in info.node.body:
        field = _field_symbol(info, statement)
        if field is not None:
            symbol, related_model = field
            symbols.append(symbol)
            relations.append(ParsedRelation(info.path, info.qualified_name, 'DECLARES_DJANGO_FIELD', symbol.qualified_name, symbol.location['line']))
            if related_model is not None:
                relations.append(ParsedRelation(info.path, symbol.qualified_name, 'RELATES_TO', related_model, symbol.location['line']))
            continue
        if isinstance(statement, ast.ClassDef) and statement.name == 'Meta':
            meta_symbol = _meta_symbol(info, statement)
            symbols.append(meta_symbol)
            relations.append(ParsedRelation(info.path, info.qualified_name, 'HAS_DJANGO_META', meta_symbol.qualified_name, statement.lineno))
    return symbols, relations


def _field_symbol(info: _ClassInfo, statement: ast.stmt) -> tuple[ParsedSymbol, str | None] | None:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    if not isinstance(statement.targets[0], ast.Name) or not isinstance(statement.value, ast.Call):
        return None
    call_name = _expr_name(statement.value.func) or ''
    if not _is_django_field(call_name):
        return None
    field_name = statement.targets[0].id
    related_model = _related_model(statement.value)
    signature = f'field {field_name}: {call_name}; related_model={related_model or "-"}; options={_call_options(statement.value)}'
    symbol = ParsedSymbol(
        kind='DjangoModelField',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__django_field__.{field_name}',
        module_name=info.module_name,
        signature=signature,
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=info.qualified_name,
    )
    return symbol, related_model


def _meta_symbol(info: _ClassInfo, node: ast.ClassDef) -> ParsedSymbol:
    assignments = []
    for statement in node.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name):
                assignments.append(f'{target.id}={_safe_unparse(statement.value)}')
    return ParsedSymbol(
        kind='DjangoModelMeta',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__django_meta__.Meta',
        module_name=info.module_name,
        signature='Meta(' + ', '.join(sorted(assignments)) + ')',
        location=_location(node),
        content_hash=_node_hash(node),
        owner_qualified_name=info.qualified_name,
    )


def _serializer_symbol(info: _ClassInfo) -> tuple[ParsedSymbol, list[ParsedRelation]]:
    model_name = _meta_assignment(info.node, 'model')
    symbol = ParsedSymbol(
        kind='DjangoSerializer',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__django_serializer__',
        module_name=info.module_name,
        signature=f'serializer {info.qualified_name}; model={model_name or "-"}',
        location=_location(info.node),
        content_hash=_node_hash(info.node),
        owner_qualified_name=info.qualified_name,
    )
    relations = []
    if model_name:
        relations.append(ParsedRelation(info.path, symbol.qualified_name, 'SERIALIZES_MODEL', model_name, info.node.lineno))
    return symbol, relations


def _viewset_symbol(info: _ClassInfo) -> ParsedSymbol:
    queryset_model = None
    for statement in info.node.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'queryset' for target in statement.targets):
            queryset_model = _expr_name(statement.value).split('.', 1)[0] if _expr_name(statement.value) else _safe_unparse(statement.value)
    return ParsedSymbol(
        kind='DjangoViewSet',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__django_viewset__',
        module_name=info.module_name,
        signature=f'viewset {info.qualified_name}; queryset_model={queryset_model or "-"}',
        location=_location(info.node),
        content_hash=_node_hash(info.node),
        owner_qualified_name=info.qualified_name,
    )


def _signal_symbols(parsed: ParsedFile) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    for statement in ast.walk(parsed.tree) if parsed.tree is not None else []:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in statement.decorator_list:
                receiver = _receiver_decorator(decorator)
                if receiver is None:
                    continue
                signal, sender = receiver
                sender = _resolve_name(sender, parsed.module_name, parsed.imports) if sender is not None else None
                qualified_name = f'{statement.name}.__django_signal_receiver__.{_safe_key(signal)}'
                symbols.append(
                    ParsedSymbol(
                        kind='DjangoSignalReceiver',
                        path=parsed.path,
                        qualified_name=qualified_name,
                        module_name=parsed.module_name,
                        signature=f'receiver {signal}; sender={sender or "-"}; function={statement.name}',
                        location=_location(decorator),
                        content_hash=_node_hash(decorator),
                        owner_qualified_name=statement.name,
                    )
                )
                relations.append(ParsedRelation(parsed.path, qualified_name, 'RECEIVES_SIGNAL', signal, decorator.lineno))
                if sender is not None:
                    relations.append(ParsedRelation(parsed.path, qualified_name, 'SIGNAL_SENDER', sender, decorator.lineno))
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            signal_connect = _signal_connect(statement.value)
            if signal_connect is None:
                continue
            signal, receiver, sender = signal_connect
            receiver = _resolve_name(receiver, parsed.module_name, parsed.imports) if receiver is not None else None
            sender = _resolve_name(sender, parsed.module_name, parsed.imports) if sender is not None else None
            qualified_name = f'{parsed.module_name}.__django_signal_connect__.{statement.lineno}.{_safe_key(signal)}'
            symbols.append(
                ParsedSymbol(
                    kind='DjangoSignalReceiver',
                    path=parsed.path,
                    qualified_name=qualified_name,
                    module_name=parsed.module_name,
                    signature=f'connect {signal}; receiver={receiver or "-"}; sender={sender or "-"}',
                    location=_location(statement),
                    content_hash=_node_hash(statement),
                    owner_qualified_name=parsed.module_name,
                )
            )
            if receiver is not None:
                relations.append(ParsedRelation(parsed.path, qualified_name, 'CONNECTS_SIGNAL', receiver, statement.lineno))
            if sender is not None:
                relations.append(ParsedRelation(parsed.path, qualified_name, 'SIGNAL_SENDER', sender, statement.lineno))
    return symbols, relations


def _middleware_symbols(parsed: ParsedFile) -> list[ParsedSymbol]:
    symbols = []
    for statement in parsed.tree.body if parsed.tree is not None else []:
        if not _assigns_name(statement, 'MIDDLEWARE'):
            continue
        value = statement.value if isinstance(statement, ast.Assign) else None
        for index, middleware in enumerate(_literal_sequence(value)):
            symbols.append(
                ParsedSymbol(
                    kind='DjangoMiddleware',
                    path=parsed.path,
                    qualified_name=f'{parsed.module_name}.__django_middleware__.{index}.{_safe_key(middleware)}',
                    module_name=parsed.module_name,
                    signature=f'middleware {middleware}',
                    location=_location(statement),
                    content_hash=_node_hash(statement),
                    owner_qualified_name=parsed.module_name,
                )
            )
    return symbols


def _receiver_decorator(decorator: ast.expr) -> tuple[str, str | None] | None:
    if not isinstance(decorator, ast.Call):
        return None
    if (_expr_name(decorator.func) or '').rsplit('.', 1)[-1] != 'receiver':
        return None
    signal = _expr_name(decorator.args[0]) if decorator.args else None
    if signal is None:
        return None
    sender = _keyword_value(decorator, 'sender')
    return signal, sender


def _signal_connect(call: ast.Call) -> tuple[str, str | None, str | None] | None:
    call_name = _expr_name(call.func) or ''
    if not call_name.endswith('.connect'):
        return None
    signal = call_name.rsplit('.', 1)[0]
    receiver = _expr_name(call.args[0]) if call.args else None
    sender = _keyword_value(call, 'sender')
    return signal, receiver, sender


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
    external_bases: set[str],
) -> list[str]:
    targets: list[str] = []
    for target in _resolved_base_targets(info, reference_index):
        target_key = reference_index.get(target)
        if target_key in model_keys or target in external_bases or target.rsplit('.', 1)[-1] in {item.rsplit('.', 1)[-1] for item in external_bases}:
            targets.append(target)
    return list(dict.fromkeys(targets))


def _reference_index(class_index: dict[tuple[str, str], _ClassInfo]) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for key, info in sorted(class_index.items(), key=lambda item: item[0]):
        index.setdefault(info.qualified_name, key)
        index.setdefault(f'{info.module_name}.{info.qualified_name}', key)
    return index


def _include_target(node: ast.AST | None, imports: dict[str, str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if (_expr_name(node.func) or '').rsplit('.', 1)[-1] != 'include':
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    raw = _expr_name(first)
    return _import_target(raw, imports) if raw is not None else None


def _is_django_field(call_name: str) -> bool:
    return call_name.startswith('models.') or call_name.rsplit('.', 1)[-1] in FIELD_SUFFIXES


def _related_model(call: ast.Call) -> str | None:
    call_name = _expr_name(call.func) or ''
    if call_name.rsplit('.', 1)[-1] not in {'ForeignKey', 'ManyToManyField', 'OneToOneField'}:
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return _expr_name(first) or _safe_unparse(first)


def _meta_assignment(node: ast.ClassDef, name: str) -> str | None:
    for statement in node.body:
        if not isinstance(statement, ast.ClassDef) or statement.name != 'Meta':
            continue
        for inner in statement.body:
            if not isinstance(inner, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == name for target in inner.targets):
                return _expr_name(inner.value) or _safe_unparse(inner.value)
    return None


def _call_options(call: ast.Call) -> str:
    options = [f'{keyword.arg}={_safe_unparse(keyword.value)}' for keyword in call.keywords if keyword.arg]
    return ','.join(options) or '-'


def _assigns_name(statement: ast.stmt, name: str) -> bool:
    return isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)


def _literal_sequence(node: ast.AST | None) -> list[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [
        value.value
        for value in node.elts
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]


def _keyword_value(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            return _expr_name(keyword.value) or _safe_unparse(keyword.value)
    return None


def _literal_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) <= index:
        return None
    value = call.args[index]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _join_routes(prefix: str, route: str) -> str:
    if not prefix:
        return route
    if not route:
        return prefix
    return f'{prefix.rstrip("/")}/{route.lstrip("/")}'


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


def _import_target(name: str, imports: dict[str, str]) -> str | None:
    root, _, remainder = name.partition('.')
    imported = imports.get(root)
    if imported is None:
        return None
    return f'{imported}.{remainder}' if remainder else imported


def _resolve_name(name: str, module_name: str, imports: dict[str, str]) -> str:
    root, _, remainder = name.partition('.')
    imported = imports.get(root)
    if imported is None:
        return name
    if '.' in module_name and not imported.startswith(('django.', 'rest_framework.')):
        package = module_name.rsplit('.', 1)[0]
        if imported == root and '.' not in imported:
            imported = f'{package}.{imported}'
        elif '.' in imported and not imported.startswith(f'{package}.'):
            imported = f'{package}.{imported}'
    return f'{imported}.{remainder}' if remainder else imported


def _safe_key(value: str) -> str:
    return ''.join(character if character.isalnum() else '_' for character in value).strip('_') or 'item'


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
