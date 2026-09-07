"""Celery-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with Celery app, task, and beat schedule nodes."""
    symbols = []
    relations = []

    # 1. Collect Celery Apps
    symbols.extend(_collect_celery_apps(parsed_files))

    # 2. Collect Celery Tasks
    task_symbols, task_relations = _collect_celery_tasks(parsed_files)
    symbols.extend(task_symbols)
    relations.extend(task_relations)

    # Build task name map for downstream resolution (beat and triggers)
    task_name_map = {}
    for sym in task_symbols:
        celery_name = _get_celery_name_from_sig(sym.signature)
        fq_symbol_name = f"{sym.module_name}.{sym.qualified_name}"
        task_name_map[celery_name] = fq_symbol_name
        
        py_qualname = f"{sym.module_name}.{sym.qualified_name.replace('.__celery_task__', '')}"
        task_name_map[py_qualname] = fq_symbol_name
        
        rel_name = sym.qualified_name.replace('.__celery_task__', '')
        task_name_map[rel_name] = fq_symbol_name

    # 3. Collect Beat Schedules (using task_name_map)
    beat_symbols, beat_relations = _collect_beat_schedules(parsed_files, task_name_map)
    symbols.extend(beat_symbols)
    relations.extend(beat_relations)

    # 4. Collect Task Invocations (using task_name_map)
    relations.extend(_collect_task_invocations(parsed_files, task_name_map))

    # Assign compiled symbols and relations back to their respective ParsedFiles
    for parsed in sorted(parsed_files, key=lambda item: item.path):
        module_symbols = [sym for sym in symbols if sym.path == parsed.path]
        module_relations = [rel for rel in relations if rel.path == parsed.path]
        parsed.symbols.extend(sorted(module_symbols, key=lambda item: (item.path, item.qualified_name, item.kind)))
        parsed.relations.extend(
            sorted(module_relations, key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name))
        )


def _get_celery_name_from_sig(sig: str) -> str:
    first, _, _ = sig.partition(';')
    return first.replace('task ', '', 1).strip()


def _collect_celery_apps(parsed_files: list[ParsedFile]) -> list[ParsedSymbol]:
    symbols = []
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        for statement in parsed.tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not isinstance(statement.value, ast.Call):
                continue
            func_name = _expr_name(statement.value.func)
            if func_name is None:
                continue
            resolved_func = _resolve_name(func_name, parsed.module_name, parsed.imports)
            if resolved_func != 'celery.Celery' and resolved_func.rsplit('.', 1)[-1] != 'Celery':
                continue
            for target in statement.targets:
                if not isinstance(target, ast.Name):
                    continue
                app_var = target.id
                broker = _keyword_value(statement.value, 'broker')
                backend = _keyword_value(statement.value, 'backend')
                qualified_name = f'__celery_app__.{app_var}'
                signature = f"Celery({_literal_arg(statement.value, 0) or ''}; broker={broker or '-'}; backend={backend or '-'})"
                symbols.append(
                    ParsedSymbol(
                        kind='CeleryApp',
                        path=parsed.path,
                        qualified_name=qualified_name,
                        module_name=parsed.module_name,
                        signature=signature,
                        location=_location(statement),
                        content_hash=_node_hash(statement),
                        owner_qualified_name=parsed.module_name,
                    )
                )
    return symbols


def _collect_celery_tasks(parsed_files: list[ParsedFile]) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols = []
    relations = []
    
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        
        def visit(body: list[ast.stmt], parent: str | None = None) -> None:
            for statement in body:
                if isinstance(statement, ast.ClassDef):
                    class_qualname = statement.name if parent is None else f'{parent}.{statement.name}'
                    visit(statement.body, class_qualname)
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_qualname = statement.name if parent is None else f'{parent}.{statement.name}'
                    is_task = False
                    task_decorator = None
                    for dec in statement.decorator_list:
                        dec_name = _expr_name(dec)
                        if dec_name is None:
                            continue
                        resolved_dec = _resolve_name(dec_name, parsed.module_name, parsed.imports)
                        if resolved_dec.rsplit('.', 1)[-1] in {'task', 'shared_task'}:
                            is_task = True
                            task_decorator = dec
                            break
                    if is_task and task_decorator is not None:
                        custom_name = None
                        queue = None
                        bind = False
                        options = []
                        if isinstance(task_decorator, ast.Call):
                            custom_name = _keyword_value(task_decorator, 'name')
                            queue = _keyword_value(task_decorator, 'queue')
                            bind_val = _keyword_value(task_decorator, 'bind')
                            if bind_val == 'True':
                                bind = True
                            for kw in task_decorator.keywords:
                                if kw.arg not in {'name', 'queue', 'bind'}:
                                    options.append(f'{kw.arg}={_safe_unparse(kw.value)}')
                        
                        task_name = custom_name or f'{parsed.module_name}.{func_qualname}'
                        signature = f"task {task_name}; queue={queue or '-'}; bind={str(bind).lower()}"
                        if options:
                            signature += f"; options={','.join(sorted(options))}"
                        
                        task_qualname = f'{func_qualname}.__celery_task__'
                        symbols.append(
                            ParsedSymbol(
                                kind='CeleryTask',
                                path=parsed.path,
                                qualified_name=task_qualname,
                                module_name=parsed.module_name,
                                signature=signature,
                                location=_location(statement),
                                content_hash=_node_hash(statement),
                                owner_qualified_name=func_qualname,
                            )
                        )
                        owner_qual = parent or parsed.module_name
                        relations.append(
                            ParsedRelation(
                                path=parsed.path,
                                source_qualified_name=owner_qual,
                                relation='DECLARES_CELERY_TASK',
                                target_name=task_qualname,
                                line=statement.lineno,
                            )
                        )
                        relations.append(
                            ParsedRelation(
                                path=parsed.path,
                                source_qualified_name=task_qualname,
                                relation='WRAPS_FUNCTION',
                                target_name=func_qualname,
                                line=statement.lineno,
                            )
                        )
                    visit(statement.body, func_qualname)
        
        visit(parsed.tree.body)
        
    return symbols, relations


def _collect_beat_schedules(
    parsed_files: list[ParsedFile],
    task_name_map: dict[str, str],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols = []
    relations = []
    
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        for statement in parsed.tree.body:
            is_beat = False
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    target_name = _expr_name(target)
                    if target_name and (target_name == 'CELERY_BEAT_SCHEDULE' or target_name.endswith('.beat_schedule')):
                        is_beat = True
                        break
            if is_beat and isinstance(statement.value, ast.Dict):
                dict_node = statement.value
                for key_node, val_node in zip(dict_node.keys, dict_node.values):
                    if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                        continue
                    entry_name = key_node.value
                    if not isinstance(val_node, ast.Dict):
                        continue
                    
                    task_name = None
                    schedule_repr = None
                    options = []
                    
                    for inner_k, inner_v in zip(val_node.keys, val_node.values):
                        if not isinstance(inner_k, ast.Constant) or not isinstance(inner_k.value, str):
                            continue
                        k_str = inner_k.value
                        if k_str == 'task':
                            if isinstance(inner_v, ast.Constant) and isinstance(inner_v.value, str):
                                task_name = inner_v.value
                        elif k_str == 'schedule':
                            schedule_repr = _safe_unparse(inner_v)
                        else:
                            options.append(f'{k_str}={_safe_unparse(inner_v)}')
                    
                    if task_name is None:
                        continue
                    
                    qualified_name = f'__celery_beat__.{_safe_key(entry_name)}'
                    signature = f'beat {entry_name}; task={task_name}; schedule={schedule_repr or "-"}'
                    if options:
                        signature += f'; options={",".join(sorted(options))}'
                        
                    symbols.append(
                        ParsedSymbol(
                            kind='CeleryBeatSchedule',
                            path=parsed.path,
                            qualified_name=qualified_name,
                            module_name=parsed.module_name,
                            signature=signature,
                            location=_location(key_node),
                            content_hash=_node_hash(val_node),
                            owner_qualified_name=parsed.module_name,
                        )
                    )
                    
                    target_symbol_name = task_name_map.get(task_name, f'{task_name}.__celery_task__')
                    relations.append(
                        ParsedRelation(
                            path=parsed.path,
                            source_qualified_name=qualified_name,
                            relation='SCHEDULES_TASK',
                            target_name=target_symbol_name,
                            line=key_node.lineno,
                        )
                    )
    return symbols, relations


def _collect_task_invocations(
    parsed_files: list[ParsedFile],
    task_name_map: dict[str, str],
) -> list[ParsedRelation]:
    relations = []
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        
        class InvocationVisitor(ast.NodeVisitor):
            def __init__(self, path: str, module_name: str, imports: dict[str, str]):
                self.path = path
                self.module_name = module_name
                self.imports = imports
                self.stack = [('Module', module_name)]
                
            def visit_ClassDef(self, node: ast.ClassDef):
                qualname = node.name if self.stack[-1][0] == 'Module' else f'{self.stack[-1][1]}.{node.name}'
                self.stack.append(('Class', qualname))
                self.generic_visit(node)
                self.stack.pop()
                
            def visit_FunctionDef(self, node: ast.FunctionDef):
                qualname = node.name if self.stack[-1][0] == 'Module' else f'{self.stack[-1][1]}.{node.name}'
                self.stack.append(('Function', qualname))
                self.generic_visit(node)
                self.stack.pop()
                
            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                qualname = node.name if self.stack[-1][0] == 'Module' else f'{self.stack[-1][1]}.{node.name}'
                self.stack.append(('Function', qualname))
                self.generic_visit(node)
                self.stack.pop()
                
            def visit_Call(self, node: ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in {'delay', 'apply_async'}:
                    parent_expr = node.func.value
                    parent_name = _expr_name(parent_expr)
                    if parent_name:
                        resolved_parent = _resolve_name(parent_name, self.module_name, self.imports)
                        target_symbol_name = task_name_map.get(resolved_parent, f'{resolved_parent}.__celery_task__')
                        caller_qual = self.stack[-1][1] if self.stack[-1][0] == 'Function' else self.module_name
                        relations.append(
                            ParsedRelation(
                                path=self.path,
                                source_qualified_name=caller_qual,
                                relation='TRIGGERS_CELERY_TASK',
                                target_name=target_symbol_name,
                                line=node.lineno,
                            )
                        )
                self.generic_visit(node)
                
        visitor = InvocationVisitor(parsed.path, parsed.module_name, parsed.imports)
        visitor.visit(parsed.tree)
        
    return relations


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


def _resolve_name(name: str, module_name: str, imports: dict[str, str]) -> str:
    root, _, remainder = name.partition('.')
    imported = imports.get(root)
    if imported is None:
        return name
        
    if imported.startswith('.'):
        dots = 0
        for char in imported:
            if char == '.':
                dots += 1
            else:
                break
        rel_path = imported[dots:]
        parts = module_name.split('.')
        if len(parts) >= dots:
            base_package = '.'.join(parts[:-dots])
            if base_package:
                imported = f'{base_package}.{rel_path}'
            else:
                imported = rel_path
        else:
            imported = rel_path
            
    elif '.' in module_name and not imported.startswith(('celery.',)):
        package = module_name.rsplit('.', 1)[0]
        if imported == root and '.' not in imported:
            imported = f'{package}.{imported}'
        elif '.' in imported and not imported.startswith(f'{package}.'):
            imported = f'{package}.{imported}'
            
    return f'{imported}.{remainder}' if remainder else imported


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
