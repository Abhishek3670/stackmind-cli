"""Parsers and extractors for documents, images, and compressed archives."""

from __future__ import annotations

import csv
import io
import os
import re
import tarfile
import zlib
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from .models import (
    ArchiveMetadata,
    AttachmentType,
    DocumentMetadata,
    ImageMetadata,
)

# Known extension mappings
EXT_TO_TYPE: dict[str, AttachmentType] = {
    # Documents
    ".pdf": AttachmentType.DOCUMENT,
    ".docx": AttachmentType.DOCUMENT,
    ".doc": AttachmentType.DOCUMENT,
    ".txt": AttachmentType.DOCUMENT,
    ".md": AttachmentType.DOCUMENT,
    ".markdown": AttachmentType.DOCUMENT,
    ".rst": AttachmentType.DOCUMENT,
    ".rtf": AttachmentType.DOCUMENT,
    ".html": AttachmentType.DOCUMENT,
    ".htm": AttachmentType.DOCUMENT,
    # Data
    ".csv": AttachmentType.DATA,
    ".tsv": AttachmentType.DATA,
    ".json": AttachmentType.DATA,
    ".yaml": AttachmentType.DATA,
    ".yml": AttachmentType.DATA,
    ".xml": AttachmentType.DATA,
    ".sql": AttachmentType.DATA,
    # Images
    ".png": AttachmentType.IMAGE,
    ".jpg": AttachmentType.IMAGE,
    ".jpeg": AttachmentType.IMAGE,
    ".webp": AttachmentType.IMAGE,
    ".gif": AttachmentType.IMAGE,
    ".bmp": AttachmentType.IMAGE,
    ".svg": AttachmentType.IMAGE,
    ".ico": AttachmentType.IMAGE,
    # Archives
    ".zip": AttachmentType.ARCHIVE,
    ".tar": AttachmentType.ARCHIVE,
    ".gz": AttachmentType.ARCHIVE,
    ".tgz": AttachmentType.ARCHIVE,
    ".bz2": AttachmentType.ARCHIVE,
    # Code
    ".py": AttachmentType.CODE,
    ".ts": AttachmentType.CODE,
    ".js": AttachmentType.CODE,
    ".dart": AttachmentType.CODE,
    ".go": AttachmentType.CODE,
    ".rs": AttachmentType.CODE,
    ".java": AttachmentType.CODE,
    ".c": AttachmentType.CODE,
    ".cpp": AttachmentType.CODE,
    ".h": AttachmentType.CODE,
}


def detect_attachment_type(filename: str, header_bytes: bytes = b"") -> AttachmentType:
    """Classify file type by suffix and magic numbers."""
    lower_name = filename.lower()
    for ext, att_type in EXT_TO_TYPE.items():
        if lower_name.endswith(ext):
            return att_type

    # Magic byte inspection for files with missing/generic extensions
    if header_bytes.startswith(b"%PDF"):
        return AttachmentType.DOCUMENT
    if header_bytes.startswith(b"PK\x03\x04"):
        return AttachmentType.ARCHIVE
    if header_bytes.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")):
        return AttachmentType.IMAGE

    return AttachmentType.OTHER


def _decode_text_bytes(raw: bytes) -> str:
    """Robustly decode raw bytes across common text encodings."""
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252", "ascii"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Document Extractors
# ---------------------------------------------------------------------------


def _extract_docx_text_and_tables(data: bytes) -> tuple[str, int, list[str]]:
    """Zero-dependency DOCX text and table extractor via standard zipfile and XML parsing."""
    import xml.etree.ElementTree as ET

    paragraphs: list[str] = []
    headings: list[str] = []
    table_count = 0

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            if "word/document.xml" not in zf.namelist():
                return "", 0, []
            xml_content = zf.read("word/document.xml")

        root = ET.fromstring(xml_content)
        # XML namespace for WordML
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        # Extract body elements (paragraphs and tables)
        body = root.find("w:body", ns)
        if body is None:
            return "", 0, []

        for elem in body:
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "p":
                # Paragraph
                text_pieces = [t.text for t in elem.findall(".//w:t", ns) if t.text]
                p_text = "".join(text_pieces).strip()
                if p_text:
                    # Check if paragraph has heading style
                    p_style = elem.find(".//w:pStyle", ns)
                    val = p_style.attrib.get(f"{{{ns['w']}}}val", "") if p_style is not None else ""
                    if "heading" in val.lower() or "title" in val.lower():
                        headings.append(p_text)
                        paragraphs.append(f"### {p_text}")
                    else:
                        paragraphs.append(p_text)
            elif tag == "tbl":
                # Table
                table_count += 1
                table_rows: list[list[str]] = []
                for row_elem in elem.findall("w:tr", ns):
                    cells: list[str] = []
                    for cell_elem in row_elem.findall("w:tc", ns):
                        cell_text = "".join(
                            [t.text for t in cell_elem.findall(".//w:t", ns) if t.text]
                        ).strip()
                        cells.append(cell_text.replace("\n", " "))
                    if cells:
                        table_rows.append(cells)

                if table_rows:
                    col_count = max(len(r) for r in table_rows)
                    # Normalize row widths
                    norm_rows = [r + [""] * (col_count - len(r)) for r in table_rows]
                    # Format as Markdown table
                    header = norm_rows[0]
                    md_table_lines = [
                        "| " + " | ".join(header) + " |",
                        "| " + " | ".join(["---"] * col_count) + " |",
                    ]
                    for r in norm_rows[1:]:
                        md_table_lines.append("| " + " | ".join(r) + " |")
                    paragraphs.append("\n" + "\n".join(md_table_lines) + "\n")

        full_text = "\n\n".join(paragraphs)
        return full_text, table_count, headings
    except Exception:
        # Fallback: regex extraction of all <w:t> pieces
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                if "word/document.xml" in zf.namelist():
                    xml_str = zf.read("word/document.xml").decode("utf-8", errors="ignore")
                    text_pieces = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml_str)
                    clean_text = " ".join(t.strip() for t in text_pieces if t.strip())
                    return clean_text, 0, []
        except Exception:
            pass
        return "", 0, []


def _extract_pdf_text(data: bytes) -> tuple[str, int]:
    """Extract plain text from PDF using pypdf if available, or native stream parser."""
    # 1. Try pypdf if installed
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        page_texts: list[str] = []
        for p in reader.pages:
            t = p.extract_text()
            if t:
                page_texts.append(t.strip())
        return "\n\n".join(page_texts), len(reader.pages)
    except (ImportError, Exception):
        pass

    # 2. Pure Python fallback: extract text streams from PDF objects
    try:
        pages = max(1, len(re.findall(rb"/Type\s*/Page\b", data)))
        text_fragments: list[str] = []

        # Find stream objects
        streams = re.findall(rb"stream[\r\n]+(.*?)[\r\n]+endstream", data, re.DOTALL)
        for s in streams:
            decompressed = None
            try:
                decompressed = zlib.decompress(s)
            except Exception:
                decompressed = s

            if not decompressed:
                continue

            # Look for BT ... ET blocks
            bt_blocks = re.findall(rb"BT[\r\n]+(.*?)[\r\n]+ET", decompressed, re.DOTALL)
            for block in bt_blocks:
                # Extract text within parentheses e.g. (Hello World) Tj
                string_matches = re.findall(rb"\((.*?)\)\s*T[jJ]", block)
                for sm in string_matches:
                    clean = _decode_text_bytes(sm).strip()
                    if clean:
                        text_fragments.append(clean)

        extracted = " ".join(text_fragments)
        return extracted, pages
    except Exception:
        return "", 1


def parse_document(raw_bytes: bytes, filename: str) -> tuple[str, DocumentMetadata]:
    """Parse document bytes into markdown/text and metadata."""
    lower = filename.lower()
    ext = Path(filename).suffix.lower()

    if lower.endswith(".docx"):
        text, tables, headings = _extract_docx_text_and_tables(raw_bytes)
        words = len(text.split()) if text else 0
        meta = DocumentMetadata(
            format="docx",
            word_count=words,
            character_count=len(text),
            page_count=max(1, words // 350),
            tables_count=tables,
            headings=headings,
        )
        return text, meta

    if lower.endswith(".pdf"):
        text, pages = _extract_pdf_text(raw_bytes)
        words = len(text.split()) if text else 0
        headings = [line for line in text.splitlines() if line.startswith("#")]
        meta = DocumentMetadata(
            format="pdf",
            word_count=words,
            character_count=len(text),
            page_count=pages,
            tables_count=0,
            headings=headings,
        )
        return text, meta

    if lower.endswith(".csv"):
        text_content = _decode_text_bytes(raw_bytes)
        reader = csv.reader(io.StringIO(text_content))
        rows = list(reader)
        lines: list[str] = []
        if rows:
            header = rows[0]
            col_count = len(header)
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * col_count) + " |")
            for r in rows[1:101]:  # limit to first 100 rows for context
                padded = r + [""] * (col_count - len(r))
                lines.append("| " + " | ".join(padded[:col_count]) + " |")
            if len(rows) > 101:
                lines.append(f"\n*(... {len(rows) - 101} more rows truncated for context)*")
        markdown_csv = "\n".join(lines)
        meta = DocumentMetadata(
            format="csv",
            word_count=len(markdown_csv.split()),
            character_count=len(markdown_csv),
            page_count=1,
            tables_count=1,
            headings=[rows[0][0]] if rows and rows[0] else [],
        )
        return markdown_csv, meta

    # Plain text / Markdown / Code / Config files
    text = _decode_text_bytes(raw_bytes)
    headings = [line.strip() for line in text.splitlines() if line.startswith("#")]
    words = len(text.split()) if text else 0
    meta = DocumentMetadata(
        format=ext.lstrip(".") or "txt",
        word_count=words,
        character_count=len(text),
        page_count=max(1, words // 350),
        tables_count=text.count("| --- |"),
        headings=headings,
    )
    return text, meta


# ---------------------------------------------------------------------------
# Image Extractor
# ---------------------------------------------------------------------------


def parse_image(raw_bytes: bytes, filename: str) -> tuple[str, ImageMetadata]:
    """Inspect image dimensions, format, and synthesize descriptive Markdown grounding."""
    file_size = len(raw_bytes)
    width = 0
    height = 0
    img_format = Path(filename).suffix.lstrip(".").upper() or "IMAGE"
    color_mode = "RGB"

    # Use Pillow if available
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw_bytes))
        width, height = img.size
        img_format = (img.format or img_format).upper()
        color_mode = img.mode
    except Exception:
        # Basic header fallback for common formats if PIL fails
        if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(raw_bytes) >= 24:
            import struct

            width, height = struct.unpack(">II", raw_bytes[16:24])
            img_format = "PNG"
        elif raw_bytes.startswith(b"GIF8") and len(raw_bytes) >= 10:
            import struct

            width, height = struct.unpack("<HH", raw_bytes[6:10])
            img_format = "GIF"

    # Aspect ratio calculation
    if width > 0 and height > 0:
        frac = Fraction(width, height).limit_denominator(20)
        aspect_ratio = f"{frac.numerator}:{frac.denominator}"
    else:
        aspect_ratio = "unknown"

    meta = ImageMetadata(
        format=img_format.lower(),
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        color_mode=color_mode,
        file_size_bytes=file_size,
        alt_description=f"{width}x{height} {img_format} Image ({aspect_ratio})",
    )

    size_kb = file_size / 1024.0
    text_desc = (
        f"### Image Attachment: `{filename}`\n"
        f"- **Format:** {img_format} | **Dimensions:** {width}x{height} px ({aspect_ratio})\n"
        f"- **Color Space:** {color_mode} | **File Size:** {size_kb:.1f} KB\n"
        f"- **Usage Note:** Verified visual deliverable / design asset available in workspace attachments.\n"
    )
    return text_desc, meta


# ---------------------------------------------------------------------------
# Archive Extractor & Zip-Slip Protection
# ---------------------------------------------------------------------------


def safe_extract_archive(
    archive_bytes: bytes,
    filename: str,
    target_dir: Path,
    max_total_uncompressed_bytes: int = 100 * 1024 * 1024,
) -> tuple[str, ArchiveMetadata, list[Path]]:
    """Securely extract zip or tar archive preventing path traversal (Zip Slip vulnerability)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_target = target_dir.resolve()

    extracted_paths: list[Path] = []
    total_uncompressed = 0
    extracted_rel_names: list[str] = []
    doc_count = 0
    img_count = 0
    code_count = 0

    lower = filename.lower()
    is_tar = lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2"))

    if is_tar:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue

                # Zip Slip sanitization
                member_clean = member.name.replace("\\", "/").lstrip("/")
                if ".." in member_clean.split("/"):
                    continue

                dest_path = (resolved_target / member_clean).resolve()
                if not str(dest_path).startswith(str(resolved_target)):
                    continue

                total_uncompressed += member.size
                if total_uncompressed > max_total_uncompressed_bytes:
                    raise ValueError(f"Archive exceeds maximum uncompressed size ({max_total_uncompressed_bytes} bytes).")

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                f_obj = tf.extractfile(member)
                if f_obj is not None:
                    dest_path.write_bytes(f_obj.read())
                    extracted_paths.append(dest_path)
                    extracted_rel_names.append(member_clean)
    else:
        # Default zipfile
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                clean_name = info.filename.replace("\\", "/").lstrip("/")
                if ".." in clean_name.split("/"):
                    continue

                dest_path = (resolved_target / clean_name).resolve()
                if not str(dest_path).startswith(str(resolved_target)):
                    continue

                total_uncompressed += info.file_size
                if total_uncompressed > max_total_uncompressed_bytes:
                    raise ValueError(f"Archive exceeds maximum uncompressed size ({max_total_uncompressed_bytes} bytes).")

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
                extracted_paths.append(dest_path)
                extracted_rel_names.append(clean_name)

    # Classify extracted members
    for p in extracted_paths:
        t = detect_attachment_type(p.name)
        if t == AttachmentType.DOCUMENT:
            doc_count += 1
        elif t == AttachmentType.IMAGE:
            img_count += 1
        elif t == AttachmentType.CODE:
            code_count += 1

    meta = ArchiveMetadata(
        format=Path(filename).suffix.lstrip(".") or "zip",
        total_files=len(extracted_paths),
        uncompressed_size_bytes=total_uncompressed,
        compressed_size_bytes=len(archive_bytes),
        extracted_members=extracted_rel_names[:50],  # first 50 member names
        document_count=doc_count,
        image_count=img_count,
        code_count=code_count,
    )

    summary_lines = [
        f"### Archive Extracted: `{filename}`",
        f"- **Total Files:** {len(extracted_paths)} ({doc_count} docs, {img_count} images, {code_count} code files)",
        f"- **Uncompressed Size:** {total_uncompressed / (1024 * 1024):.2f} MB",
        "- **Extracted Contents Preview:**",
    ]
    for m in extracted_rel_names[:15]:
        summary_lines.append(f"  • `{m}`")
    if len(extracted_rel_names) > 15:
        summary_lines.append(f"  *(and {len(extracted_rel_names) - 15} additional files)*")

    return "\n".join(summary_lines), meta, extracted_paths
