"""Test compiler frontend augmentation for the knowledge compiler.

Discovers pytest/unittest suites, test cases, fixtures, and maps test coverage
and target relationships into the knowledge graph.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with test suite, test case, fixture, and coverage nodes."""
    for parsed in parsed_files:
        if not _is_test_file(parsed.path):
            continue
        if parsed.tree is None:
            continue

        symbols, relations = _compile_test_file(parsed)
        parsed.symbols.extend(symbols)
        parsed.relations.extend(relations)

    # If project_path provided, check for .coverage file
    if project_path is not None:
        coverage_db = project_path / ".coverage"
        if coverage_db.exists():
            _compile_coverage_db(coverage_db, parsed_files, project_path)


def _is_test_file(path: str) -> bool:
    lower = path.lower()
    return (
        lower.startswith("tests/")
        or "/tests/" in lower
        or Path(path).name.startswith("test_")
        or Path(path).stem.endswith("_test")
    )


def _compile_test_file(parsed: ParsedFile) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    # Map target source file / module
    target_module = _infer_target_module(parsed.path)

    for symbol in parsed.symbols:
        # Check if Class symbol is a TestSuite
        if symbol.kind == "Class" and (symbol.qualified_name.startswith("Test") or "Test" in symbol.qualified_name):
            suite_qualname = f"testsuite:{parsed.path}:{symbol.qualified_name}"
            symbols.append(
                ParsedSymbol(
                    kind="TestSuite",
                    path=parsed.path,
                    qualified_name=suite_qualname,
                    module_name=parsed.module_name,
                    signature=f"suite {symbol.qualified_name}",
                    location=symbol.location,
                    content_hash=symbol.content_hash,
                    owner_qualified_name=symbol.qualified_name,
                )
            )

        # Check if Function or Method is a TestCase
        elif symbol.kind in {"Function", "Method"} and (
            symbol.qualified_name.split(".")[-1].startswith("test_")
            or symbol.qualified_name.split(".")[-1].startswith("test")
        ):
            case_qualname = f"testcase:{parsed.path}:{symbol.qualified_name}"
            symbols.append(
                ParsedSymbol(
                    kind="TestCase",
                    path=parsed.path,
                    qualified_name=case_qualname,
                    module_name=parsed.module_name,
                    signature=symbol.signature,
                    location=symbol.location,
                    content_hash=symbol.content_hash,
                    owner_qualified_name=symbol.owner_qualified_name,
                )
            )

            # If inside a class, relate to TestSuite
            if symbol.owner_qualified_name:
                suite_qualname = f"testsuite:{parsed.path}:{symbol.owner_qualified_name}"
                relations.append(
                    ParsedRelation(
                        path=parsed.path,
                        source_qualified_name=suite_qualname,
                        relation="SUITE_CONTAINS_CASE",
                        target_name=case_qualname,
                        line=symbol.location.get("line", 1),
                    )
                )

            # Relate TestCase to Target Module
            if target_module:
                relations.append(
                    ParsedRelation(
                        path=parsed.path,
                        source_qualified_name=case_qualname,
                        relation="TESTS_SYMBOL",
                        target_name=target_module,
                        line=symbol.location.get("line", 1),
                        confidence=0.85,
                    )
                )

        # Check if Function is a Pytest Fixture
        elif symbol.kind == "Function" and _has_fixture_decorator(parsed.tree, symbol.qualified_name):
            fixture_qualname = f"fixture:{parsed.path}:{symbol.qualified_name}"
            symbols.append(
                ParsedSymbol(
                    kind="TestFixture",
                    path=parsed.path,
                    qualified_name=fixture_qualname,
                    module_name=parsed.module_name,
                    signature=symbol.signature,
                    location=symbol.location,
                    content_hash=symbol.content_hash,
                    owner_qualified_name=None,
                )
            )

    return symbols, relations


def _has_fixture_decorator(tree: ast.Module, func_name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for decorator in node.decorator_list:
                dec_name = ""
                if isinstance(decorator, ast.Name):
                    dec_name = decorator.id
                elif isinstance(decorator, ast.Attribute):
                    dec_name = decorator.attr
                elif isinstance(decorator, ast.Call):
                    if isinstance(decorator.func, ast.Name):
                        dec_name = decorator.func.id
                    elif isinstance(decorator.func, ast.Attribute):
                        dec_name = decorator.func.attr
                if dec_name == "fixture":
                    return True
    return False


def _infer_target_module(test_path: str) -> str | None:
    filename = Path(test_path).name
    if filename.startswith("test_"):
        target_name = filename[5:-3]  # strip test_ and .py
    elif filename.endswith("_test.py"):
        target_name = filename[:-8]
    else:
        return None
    return target_name


def _compile_coverage_db(coverage_path: Path, parsed_files: list[ParsedFile], project_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(coverage_path))
        cursor = conn.cursor()
        cursor.execute("SELECT path FROM file")
        rows = cursor.fetchall()
        conn.close()

        for (file_path,) in rows:
            try:
                rel = Path(file_path).resolve().relative_to(project_path).as_posix()
            except Exception:
                continue

            for parsed in parsed_files:
                if parsed.path == rel:
                    parsed.relations.append(
                        ParsedRelation(
                            path=rel,
                            source_qualified_name=parsed.module_name,
                            relation="COVERAGE_COVERS",
                            target_name=rel,
                            line=1,
                            confidence=1.0,
                        )
                    )
    except Exception:
        pass
