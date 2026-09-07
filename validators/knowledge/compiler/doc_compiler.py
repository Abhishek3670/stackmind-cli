"""Documentation compiler frontend augmentation for the knowledge compiler.

Parses markdown files (README, RFC, ADR, docs) to extract documentation nodes,
sections, links, and symbol cross-references into the knowledge graph.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .parse import DEFAULT_EXCLUDED_DIRS, ParsedFile, ParsedRelation, ParsedSymbol

EXCLUDED_DIRS = DEFAULT_EXCLUDED_DIRS

LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")
CODE_REF_PATTERN = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_\.]*)`")


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with markdown documentation symbols and relations."""
    doc_paths: list[tuple[str, Path | None]] = []

    if project_path is not None:
        project_path = project_path.resolve()
        for path in sorted(project_path.rglob("*.md")):
            if not any(part in EXCLUDED_DIRS for part in path.relative_to(project_path).parts):
                rel_str = path.relative_to(project_path).as_posix()
                doc_paths.append((rel_str, path))

    # Also check if any .md files are already in parsed_files
    existing_paths = {p.path for p in parsed_files}
    parsed_by_path = {p.path: p for p in parsed_files}

    for rel_path, full_path in doc_paths:
        if full_path is None or not full_path.exists():
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        symbols, relations = _compile_markdown(rel_path, content)
        if not symbols:
            continue

        if rel_path in parsed_by_path:
            parsed = parsed_by_path[rel_path]
            parsed.symbols.extend(symbols)
            parsed.relations.extend(relations)
        else:
            module_name = rel_path.replace("/", ".").rstrip(".md")
            parsed_file = ParsedFile(
                path=rel_path,
                module_name=module_name,
                tree=None,
                symbols=symbols,
                relations=relations,
            )
            parsed_files.append(parsed_file)
            parsed_by_path[rel_path] = parsed_file


def _compile_markdown(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.replace("\r\n", "\n").encode("utf-8")).hexdigest()

    # Determine title & document kind
    title = _extract_title(rel_path, lines)
    lower_path = rel_path.lower()
    lower_title = title.lower()

    if "adr" in lower_path or lower_title.startswith("adr"):
        doc_kind = "ADR"
        qual_prefix = f"doc.adr:{rel_path}"
    elif "rfc" in lower_path or lower_title.startswith("rfc"):
        doc_kind = "RFC"
        qual_prefix = f"doc.rfc:{rel_path}"
    else:
        doc_kind = "DocFile"
        qual_prefix = f"doc:{rel_path}"

    doc_symbol = ParsedSymbol(
        kind=doc_kind,
        path=rel_path,
        qualified_name=qual_prefix,
        module_name=rel_path.replace("/", ".").rstrip(".md"),
        signature=f"doc {title}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(doc_symbol)

    # Parse sections and links
    for idx, line in enumerate(lines, start=1):
        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            slug = _slugify(heading_text)
            section_qualname = f"{qual_prefix}#{slug}"

            section_symbol = ParsedSymbol(
                kind="DocSection",
                path=rel_path,
                qualified_name=section_qualname,
                module_name=rel_path.replace("/", ".").rstrip(".md"),
                signature=f"h{level} {heading_text}",
                location={"line": idx, "column": 0, "end_line": idx, "end_column": len(line)},
                content_hash=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                owner_qualified_name=qual_prefix,
            )
            symbols.append(section_symbol)

            relations.append(
                ParsedRelation(
                    path=rel_path,
                    source_qualified_name=qual_prefix,
                    relation="CONTAINS_SECTION",
                    target_name=section_qualname,
                    line=idx,
                    confidence=1.0,
                )
            )

        # Markdown links
        for link_text, link_target in LINK_PATTERN.findall(line):
            if link_target.startswith("http://") or link_target.startswith("https://"):
                continue
            target_clean = link_target.split("#")[0].strip()
            if target_clean:
                target_rel = _normalize_relative_path(rel_path, target_clean)
                relations.append(
                    ParsedRelation(
                        path=rel_path,
                        source_qualified_name=qual_prefix,
                        relation="DOC_LINKS_TO",
                        target_name=f"doc:{target_rel}",
                        line=idx,
                        confidence=0.9,
                    )
                )

        # Code references inside backticks
        for code_ref in CODE_REF_PATTERN.findall(line):
            if "." in code_ref or len(code_ref) > 3:
                relations.append(
                    ParsedRelation(
                        path=rel_path,
                        source_qualified_name=qual_prefix,
                        relation="DOCUMENTS_SYMBOL",
                        target_name=code_ref,
                        line=idx,
                        confidence=0.8,
                    )
                )

    return symbols, relations


def _extract_title(rel_path: str, lines: list[str]) -> str:
    for line in lines:
        match = HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return Path(rel_path).stem


def _slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[-\s]+", "-", slug).strip("-") or "section"


def _normalize_relative_path(current_file: str, target_link: str) -> str:
    curr_parent = Path(current_file).parent
    resolved = (curr_parent / target_link).resolve()
    # If target is relative, strip leading slashes
    target_clean = target_link.lstrip("./").lstrip("/")
    return target_clean
