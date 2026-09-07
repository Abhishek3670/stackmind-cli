"""Tests for WO-021 Documentation Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.doc_compiler import augment_parsed_files


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_doc_compiler_parses_readme_and_sections(tmp_path):
    project = tmp_path / "doc-project"
    init(project, name="Doc Project", no_git=True)

    readme_content = (
        "# Main Documentation\n\n"
        "Welcome to the project.\n\n"
        "## Architecture Overview\n\n"
        "Detailed architecture overview.\n\n"
        "### Component Model\n\n"
        "Component description.\n\n"
        "See also [Architecture Doc](docs/arch.md) and `cli.main.cli`.\n"
    )
    _write(project, "README.md", readme_content)
    _write(project, "docs/arch.md", "# Architecture\n\nDetails here.\n")

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check doc symbols
    doc_symbols = [s for s in symbols if s["kind"] in {"DocFile", "DocSection"}]
    assert any(s["qualified_name"] == "doc:README.md" for s in doc_symbols)
    assert any("architecture-overview" in s["qualified_name"] for s in doc_symbols)

    # Check CONTAINS_SECTION and DOC_LINKS_TO relations
    assert any(e["relation"] == "CONTAINS_SECTION" for e in edges)
    assert any(e["relation"] == "DOC_LINKS_TO" for e in edges)


def test_doc_compiler_detects_adr_and_rfc(tmp_path):
    project = tmp_path / "adr-project"
    init(project, name="ADR Project", no_git=True)

    _write(project, "docs/adr/0001-record.md", "# ADR 0001: System Lock\n\nDecision details.\n")
    _write(project, "rfc/rfc-002.md", "# RFC 002: Knowledge API\n\nRequest for comments.\n")

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]

    assert any(s["kind"] == "ADR" for s in symbols)
    assert any(s["kind"] == "RFC" for s in symbols)
