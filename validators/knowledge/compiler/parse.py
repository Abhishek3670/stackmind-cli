"""Deterministic Python parser for the knowledge compiler frontend.

LibCST is the preferred parser in production because it preserves full source
fidelity. The local runtime may not have it installed, so this module keeps an
AST fallback with the same deterministic output contract.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_EXCLUDED_DIRS = frozenset({
    ".git",
    ".sync",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "site-packages",
    "build",
    "dist",
    ".pytest-tmp",
})


@dataclass(frozen=True)
class ParsedSymbol:
    """A source definition discovered by the parser."""

    kind: str
    path: str
    qualified_name: str
    module_name: str
    signature: str
    location: dict[str, int]
    content_hash: str
    owner_qualified_name: str | None


@dataclass(frozen=True)
class ParsedCall:
    """A call expression discovered by the parser."""

    path: str
    source_qualified_name: str
    target_name: str
    line: int


@dataclass(frozen=True)
class ParsedRelation:
    """A deterministic non-call relationship discovered by a frontend."""

    path: str
    source_qualified_name: str
    relation: str
    target_name: str
    line: int
    confidence: float = 1.0


@dataclass(frozen=True)
class ParsedDiagnostic:
    """A parser diagnostic that does not abort the batch."""

    path: str
    code: str
    message: str
    line: int | None = None


@dataclass
class ParsedFile:
    """Parse output for one Python file."""

    path: str
    module_name: str
    tree: ast.Module | None = None
    symbols: list[ParsedSymbol] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)
    relations: list[ParsedRelation] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)
    diagnostics: list[ParsedDiagnostic] = field(default_factory=list)


def discover_python_files(root: Path) -> list[Path]:
    """Return project Python files in deterministic order."""
    excluded = {".git", ".sync", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv", ".venv", "env", ".env", "node_modules", ".tox", "site-packages", ".pytest-tmp"}
    files = [
        p for p in root.rglob("*.py")
        if not any(part in excluded for part in p.relative_to(root).parts)
    ]
    return sorted(files, key=lambda item: _rel_path(root, item))


def parse_project(root: Path) -> list[ParsedFile]:
    """Parse all Python files under a project root."""
    root = root.resolve()
    return [parse_file(path, root) for path in discover_python_files(root)]


def parse_file(path: Path, root: Path) -> ParsedFile:
    """Parse one Python file, emitting diagnostics instead of raising syntax errors."""
    rel_path = _rel_path(root, path)
    module_name = _module_name(rel_path)
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return ParsedFile(
            path=rel_path,
            module_name=module_name,
            diagnostics=[
                ParsedDiagnostic(
                    path=rel_path,
                    code="READ_ERROR",
                    message=str(exc),
                )
            ],
        )

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return ParsedFile(
            path=rel_path,
            module_name=module_name,
            diagnostics=[
                ParsedDiagnostic(
                    path=rel_path,
                    code="PARSE_ERROR",
                    message=exc.msg,
                    line=exc.lineno,
                )
            ],
        )

    visitor = _ParserVisitor(rel_path, module_name, source)
    visitor.visit(tree)
    module_symbol = _module_symbol(rel_path, module_name, source, tree)
    return ParsedFile(
        path=rel_path,
        module_name=module_name,
        tree=tree,
        symbols=[module_symbol, *visitor.symbols],
        calls=visitor.calls,
        imports=dict(sorted(visitor.imports.items())),
    )


class _ParserVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, module_name: str, source: str) -> None:
        self.rel_path = rel_path
        self.module_name = module_name
        self.source = source
        self.symbols: list[ParsedSymbol] = []
        self.calls: list[ParsedCall] = []
        self.imports: dict[str, str] = {}
        self.stack: list[tuple[str, str]] = [("Module", module_name)]

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in sorted(node.names, key=lambda item: item.asname or item.name):
            local = alias.asname or alias.name.split(".", 1)[0]
            self.imports[local] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = "." * node.level + (node.module or "")
        for alias in sorted(node.names, key=lambda item: item.asname or item.name):
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.imports[local] = f"{module}.{alias.name}".strip(".")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        qualname = self._child_qualname(node.name)
        self.symbols.append(self._symbol("Class", qualname, node))
        self.stack.append(("Class", qualname))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> Any:
        target = _call_name(node.func)
        if target:
            self.calls.append(
                ParsedCall(
                    path=self.rel_path,
                    source_qualified_name=self.stack[-1][1],
                    target_name=target,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        qualname = self._child_qualname(node.name)
        owner_kind = self.stack[-1][0]
        kind = "Method" if owner_kind == "Class" else "Function"
        self.symbols.append(self._symbol(kind, qualname, node, is_async=is_async))
        self.stack.append((kind, qualname))
        self.generic_visit(node)
        self.stack.pop()

    def _child_qualname(self, name: str) -> str:
        parent_kind, parent_name = self.stack[-1]
        if parent_kind == "Module":
            return name
        return f"{parent_name}.{name}"

    def _symbol(
        self,
        kind: str,
        qualname: str,
        node: ast.AST,
        *,
        is_async: bool = False,
    ) -> ParsedSymbol:
        return ParsedSymbol(
            kind=kind,
            path=self.rel_path,
            qualified_name=qualname,
            module_name=self.module_name,
            signature=_signature(node, is_async=is_async),
            location=_location(node),
            content_hash=_symbol_content_hash(node),
            owner_qualified_name=None if self.stack[-1][0] == "Module" else self.stack[-1][1],
        )


def _module_symbol(rel_path: str, module_name: str, source: str, tree: ast.Module) -> ParsedSymbol:
    last_line = len(source.splitlines()) or 1
    return ParsedSymbol(
        kind="Module",
        path=rel_path,
        qualified_name=module_name,
        module_name=module_name,
        signature="",
        location={"line": 1, "column": 0, "end_line": last_line, "end_column": 0},
        content_hash=_content_hash(ast.dump(tree, include_attributes=False)),
        owner_qualified_name=None,
    )


def _signature(node: ast.AST, *, is_async: bool = False) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ""
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    args = []
    for arg in [*node.args.posonlyargs, *node.args.args]:
        args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    for arg in node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    prefix = "async def" if is_async else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def _symbol_content_hash(node: ast.AST) -> str:
    normalized = copy.deepcopy(node)
    if isinstance(normalized, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        normalized.name = "__symbol__"
    return _content_hash(ast.dump(normalized, include_attributes=False))


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return None


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "column": int(getattr(node, "col_offset", 0)),
        "end_column": int(getattr(node, "end_col_offset", 0) or 0),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1),
        "line": int(getattr(node, "lineno", 1) or 1),
    }


def _content_hash(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _rel_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _module_name(rel_path: str) -> str:
    path = Path(rel_path)
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem
