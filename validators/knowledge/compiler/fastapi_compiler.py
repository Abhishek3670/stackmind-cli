"""FastAPI-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from .parse import ParsedFile, ParsedRelation, ParsedSymbol

HTTP_METHODS = {'delete', 'get', 'head', 'options', 'patch', 'post', 'put', 'trace'}
AUTH_CALLS = {
    'APIKeyCookie',
    'APIKeyHeader',
    'APIKeyQuery',
    'HTTPBasic',
    'HTTPBearer',
    'OAuth2',
    'OAuth2AuthorizationCodeBearer',
    'OAuth2PasswordBearer',
    'OpenIdConnect',
}


@dataclass(frozen=True)
class _FunctionInfo:
    path: str
    module_name: str
    qualified_name: str
    imports: dict[str, str]
    symbol: ParsedSymbol
    node: ast.FunctionDef | ast.AsyncFunctionDef


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with FastAPI route, dependency, and middleware nodes."""
    functions = _collect_functions(parsed_files)
    for parsed in sorted(parsed_files, key=lambda item: item.path):
        if parsed.tree is None:
            continue
        symbols, relations = _compile_file(parsed, functions)
        parsed.symbols.extend(symbols)
        parsed.relations.extend(relations)


def _collect_functions(parsed_files: list[ParsedFile]) -> dict[tuple[str, str], _FunctionInfo]:
    functions: dict[tuple[str, str], _FunctionInfo] = {}
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        function_symbols = {
            symbol.qualified_name: symbol
            for symbol in parsed.symbols
            if symbol.kind in {'Function', 'Method'}
        }
        for qualified_name, node in _walk_functions(parsed.tree):
            symbol = function_symbols.get(qualified_name)
            if symbol is None:
                continue
            functions[(parsed.path, qualified_name)] = _FunctionInfo(
                path=parsed.path,
                module_name=parsed.module_name,
                qualified_name=qualified_name,
                imports=parsed.imports,
                symbol=symbol,
                node=node,
            )
    return functions


def _walk_functions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(body: list[ast.stmt], parent: str | None = None) -> None:
        for statement in body:
            if isinstance(statement, ast.ClassDef):
                name = statement.name if parent is None else f'{parent}.{statement.name}'
                visit(statement.body, name)
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = statement.name if parent is None else f'{parent}.{statement.name}'
                found.append((name, statement))
                visit(statement.body, name)

    visit(tree.body)
    return found


def _compile_file(
    parsed: ParsedFile,
    functions: dict[tuple[str, str], _FunctionInfo],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    auth_names: set[str] = set()
    router_prefixes = _router_prefixes(parsed.tree)

    for statement in parsed.tree.body if parsed.tree is not None else []:
        auth_symbol = _auth_symbol(parsed, statement)
        if auth_symbol is not None:
            symbols.append(auth_symbol)
            auth_names.add(auth_symbol.qualified_name.rsplit('.', 1)[-1])
        middleware_symbol, middleware_relations = _middleware_symbol(parsed, statement)
        if middleware_symbol is not None:
            symbols.append(middleware_symbol)
            relations.extend(middleware_relations)

    for key, info in sorted(functions.items(), key=lambda item: item[0]):
        if key[0] != parsed.path:
            continue
        local_functions = _local_functions(parsed.path, functions)
        route_symbols, route_relations = _route_symbols(info, auth_names, router_prefixes, local_functions)
        symbols.extend(route_symbols)
        relations.extend(route_relations)

    return (
        sorted(symbols, key=lambda item: (item.path, item.qualified_name, item.kind)),
        sorted(relations, key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name)),
    )


def _route_symbols(
    info: _FunctionInfo,
    auth_names: set[str],
    router_prefixes: dict[str, str],
    local_functions: dict[str, _FunctionInfo],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    dependency_counter = 0

    for decorator in info.node.decorator_list:
        route = _route_decorator(decorator)
        if route is None:
            continue
        method, decorator_owner, path, response_model, status_code, decorator_dependencies = route
        path = _join_paths(router_prefixes.get(decorator_owner, ''), path)
        route_name = f'{info.qualified_name}.__route__.{method}.{_route_key(path)}'
        route_symbol = ParsedSymbol(
            kind='FastAPIRoute',
            path=info.path,
            qualified_name=route_name,
            module_name=info.module_name,
            signature=(
                f'{method} {path}; endpoint={info.qualified_name}; '
                f'response_model={response_model or "-"}; status_code={status_code or "-"}'
            ),
            location=_location(decorator),
            content_hash=_node_hash(decorator),
            owner_qualified_name=info.qualified_name,
        )
        symbols.append(route_symbol)
        relations.append(
            ParsedRelation(
                path=info.path,
                source_qualified_name=route_name,
                relation='HANDLES',
                target_name=info.qualified_name,
                line=getattr(decorator, 'lineno', info.node.lineno),
            )
        )
        if response_model:
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=route_name,
                    relation='USES_RESPONSE_MODEL',
                    target_name=response_model,
                    line=getattr(decorator, 'lineno', info.node.lineno),
                )
            )
        for request_model in _request_models(info.node):
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=route_name,
                    relation='USES_REQUEST_MODEL',
                    target_name=request_model,
                    line=info.node.lineno,
                )
            )

        for depends_call in [*_endpoint_dependencies(info.node), *decorator_dependencies]:
            dependency_counter += 1
            dependency_symbol, dependency_relations = _dependency_symbol(
                info,
                route_name,
                route_name,
                'ROUTE_DEPENDS_ON',
                depends_call,
                dependency_counter,
                auth_names,
            )
            symbols.append(dependency_symbol)
            relations.extend(dependency_relations)
            target = _depends_target(depends_call)
            target_function = _target_function(target, local_functions)
            if target_function is None:
                continue
            for nested_call in _endpoint_dependencies(target_function.node):
                dependency_counter += 1
                nested_symbol, nested_relations = _dependency_symbol(
                    info,
                    route_name,
                    dependency_symbol.qualified_name,
                    'DEPENDENCY_DEPENDS_ON',
                    nested_call,
                    dependency_counter,
                    auth_names,
                )
                symbols.append(nested_symbol)
                relations.extend(nested_relations)

    return symbols, relations


def _route_decorator(
    decorator: ast.expr,
) -> tuple[str, str, str, str | None, str | None, list[ast.Call]] | None:
    if not isinstance(decorator, ast.Call):
        return None
    name = _expr_name(decorator.func)
    if name is None:
        return None
    method = name.rsplit('.', 1)[-1].lower()
    if method not in HTTP_METHODS:
        return None
    owner = name.rsplit('.', 1)[0] if '.' in name else ''
    path = _literal_arg(decorator, 0)
    if path is None:
        return None
    response_model = _keyword_value(decorator, 'response_model')
    status_code = _keyword_value(decorator, 'status_code')
    dependencies = []
    for keyword in decorator.keywords:
        if keyword.arg == 'dependencies':
            dependencies.extend(_depends_calls(keyword.value))
    return method.upper(), owner, path, response_model, status_code, dependencies


def _request_models(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    models: list[str] = []
    defaults_by_name = {
        arg.arg: default
        for arg, default in zip(reversed(node.args.args), reversed(node.args.defaults))
    }
    for arg in [*node.args.args, *node.args.kwonlyargs]:
        if arg.arg in {'self', 'cls', 'request'}:
            continue
        default = defaults_by_name.get(arg.arg)
        if default is not None and _depends_calls(default):
            continue
        if arg.annotation is None:
            continue
        annotation = _expr_name(arg.annotation)
        if annotation is not None:
            models.append(annotation)
    return list(dict.fromkeys(models))


def _dependency_symbol(
    info: _FunctionInfo,
    route_name: str,
    parent_name: str,
    parent_relation: str,
    depends_call: ast.Call,
    index: int,
    auth_names: set[str],
) -> tuple[ParsedSymbol, list[ParsedRelation]]:
    target_name = _depends_target(depends_call)
    name = target_name or f'line_{getattr(depends_call, "lineno", info.node.lineno)}'
    qualified_name = f'{route_name}.__dependency__.{index}.{_safe_key(name)}'
    symbol = ParsedSymbol(
        kind='FastAPIDependency',
        path=info.path,
        qualified_name=qualified_name,
        module_name=info.module_name,
        signature=f'Depends({target_name or "-"})',
        location=_location(depends_call),
        content_hash=_node_hash(depends_call),
        owner_qualified_name=route_name,
    )
    relations = [
        ParsedRelation(
            path=info.path,
            source_qualified_name=parent_name,
            relation=parent_relation,
            target_name=qualified_name,
            line=getattr(depends_call, 'lineno', info.node.lineno),
        )
    ]
    if target_name:
        relations.append(
            ParsedRelation(
                path=info.path,
                source_qualified_name=qualified_name,
                relation='DEPENDS_TARGET',
                target_name=target_name,
                line=getattr(depends_call, 'lineno', info.node.lineno),
            )
        )
        if target_name.rsplit('.', 1)[-1] in auth_names:
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=route_name,
                    relation='USES_AUTH',
                    target_name=f'{info.module_name}.__auth__.{target_name.rsplit(".", 1)[-1]}',
                    line=getattr(depends_call, 'lineno', info.node.lineno),
                )
            )
    return symbol, relations


def _local_functions(
    path: str,
    functions: dict[tuple[str, str], _FunctionInfo],
) -> dict[str, _FunctionInfo]:
    local: dict[str, _FunctionInfo] = {}
    for (function_path, qualified_name), info in sorted(functions.items(), key=lambda item: item[0]):
        if function_path != path:
            continue
        local.setdefault(qualified_name, info)
        local.setdefault(qualified_name.rsplit('.', 1)[-1], info)
    return local


def _target_function(target_name: str | None, local_functions: dict[str, _FunctionInfo]) -> _FunctionInfo | None:
    if target_name is None:
        return None
    return local_functions.get(target_name) or local_functions.get(target_name.rsplit('.', 1)[-1])


def _endpoint_dependencies(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    dependencies: list[ast.Call] = []
    defaults = [*node.args.defaults, *node.args.kw_defaults]
    for default in defaults:
        if default is None:
            continue
        dependencies.extend(_depends_calls(default))
    return dependencies


def _depends_calls(node: ast.AST) -> list[ast.Call]:
    if isinstance(node, ast.Call):
        call_name = _expr_name(node.func)
        if call_name and call_name.rsplit('.', 1)[-1] == 'Depends':
            return [node]
    calls: list[ast.Call] = []
    for child in ast.iter_child_nodes(node):
        calls.extend(_depends_calls(child))
    return calls


def _depends_target(call: ast.Call) -> str | None:
    if call.args:
        return _expr_name(call.args[0]) or _safe_unparse(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == 'dependency':
            return _expr_name(keyword.value) or _safe_unparse(keyword.value)
    return None


def _middleware_symbol(parsed: ParsedFile, statement: ast.stmt) -> tuple[ParsedSymbol | None, list[ParsedRelation]]:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None, []
    call = statement.value
    if not (_expr_name(call.func) or '').endswith('.add_middleware'):
        return None, []
    middleware = _expr_name(call.args[0]) if call.args else None
    if middleware is None:
        middleware = _safe_unparse(call.args[0]) if call.args else 'middleware'
    qualified_name = f'{parsed.module_name}.__middleware__.{_safe_key(middleware)}.{statement.lineno}'
    symbol = ParsedSymbol(
        kind='FastAPIMiddleware',
        path=parsed.path,
        qualified_name=qualified_name,
        module_name=parsed.module_name,
        signature=f'middleware {middleware}',
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=parsed.module_name,
    )
    return symbol, [
        ParsedRelation(
            path=parsed.path,
            source_qualified_name=qualified_name,
            relation='USES_MIDDLEWARE',
            target_name=middleware,
            line=statement.lineno,
        )
    ]


def _router_prefixes(tree: ast.Module | None) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    if tree is None:
        return prefixes
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if not (_expr_name(call.func) or '').endswith('.include_router'):
            continue
        router_name = _expr_name(call.args[0]) if call.args else None
        if router_name is None:
            continue
        prefix = ''
        for keyword in call.keywords:
            if keyword.arg == 'prefix' and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    prefix = keyword.value.value
        prefixes[router_name] = prefix
    return dict(sorted(prefixes.items()))


def _auth_symbol(parsed: ParsedFile, statement: ast.stmt) -> ParsedSymbol | None:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    if not isinstance(statement.targets[0], ast.Name) or not isinstance(statement.value, ast.Call):
        return None
    call_name = _expr_name(statement.value.func)
    if call_name is None or call_name.rsplit('.', 1)[-1] not in AUTH_CALLS:
        return None
    local_name = statement.targets[0].id
    return ParsedSymbol(
        kind='FastAPIAuth',
        path=parsed.path,
        qualified_name=f'{parsed.module_name}.__auth__.{local_name}',
        module_name=parsed.module_name,
        signature=f'{local_name} = {_safe_unparse(statement.value)}',
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=parsed.module_name,
    )


def _literal_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) <= index:
        return None
    value = call.args[index]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _keyword_value(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _expr_name(keyword.value) or _safe_unparse(keyword.value)
    return None


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_name(node.value)
        return f'{parent}.{node.attr}' if parent else node.attr
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    return None


def _route_key(path: str) -> str:
    normalized = path.strip('/') or 'root'
    return _safe_key(normalized)


def _join_paths(prefix: str, path: str) -> str:
    if not prefix:
        return path
    left = prefix.rstrip('/')
    right = path if path.startswith('/') else f'/{path}'
    return f'{left}{right}' or '/'


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
