"""Unit tests for Multi-Modal Document, Image, and Zip Ingestion Engine (Pillar 4)."""

import io
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from validators.ingest import (
    AttachmentRecord,
    AttachmentStore,
    AttachmentType,
    detect_attachment_type,
    parse_document,
    parse_image,
    safe_extract_archive,
)


def _create_mock_docx(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    """Create a minimal valid DOCX byte stream with word/document.xml in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        p_xml_pieces: list[str] = []
        for p in paragraphs:
            if p.startswith("#"):
                p_xml_pieces.append(
                    f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{p.lstrip("# ")}</w:t></w:r></w:p>'
                )
            else:
                p_xml_pieces.append(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>")

        tbl_xml = ""
        if table_rows:
            tr_pieces = []
            for row in table_rows:
                tc_pieces = "".join([f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row])
                tr_pieces.append(f"<w:tr>{tc_pieces}</w:tr>")
            tbl_xml = f"<w:tbl>{''.join(tr_pieces)}</w:tbl>"

        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(p_xml_pieces)}{tbl_xml}</w:body>'
            "</w:document>"
        )
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def _create_mock_png(width: int = 200, height: int = 100) -> bytes:
    """Create a simple PNG image in memory."""
    img = Image.new("RGB", (width, height), color=(73, 109, 137))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_detect_attachment_type():
    assert detect_attachment_type("report.pdf") == AttachmentType.DOCUMENT
    assert detect_attachment_type("notes.docx") == AttachmentType.DOCUMENT
    assert detect_attachment_type("data.csv") == AttachmentType.DATA
    assert detect_attachment_type("diagram.png") == AttachmentType.IMAGE
    assert detect_attachment_type("archive.zip") == AttachmentType.ARCHIVE
    assert detect_attachment_type("bundle.tar.gz") == AttachmentType.ARCHIVE
    assert detect_attachment_type("main.py") == AttachmentType.CODE


def test_parse_markdown_and_csv_document():
    # Markdown
    md_content = b"# Project Title\n\nThis is a test documentation file with two paragraphs."
    text, meta = parse_document(md_content, "README.md")
    assert "Project Title" in text
    assert meta.format == "md"
    assert meta.word_count > 5
    assert len(meta.headings) == 1

    # CSV
    csv_content = b"Name,Role,Status\nAlice,Architect,Active\nBob,QA,Active"
    csv_text, csv_meta = parse_document(csv_content, "team.csv")
    assert "| Name | Role | Status |" in csv_text
    assert "| Alice | Architect | Active |" in csv_text
    assert csv_meta.tables_count == 1


def test_parse_docx_document():
    docx_bytes = _create_mock_docx(
        paragraphs=["# Architecture Overview", "StackMind governs multi-agent execution."],
        table_rows=[["ID", "Name"], ["WO-01", "Governance"]],
    )
    text, meta = parse_document(docx_bytes, "architecture.docx")
    assert "### Architecture Overview" in text
    assert "StackMind governs multi-agent execution." in text
    assert "| ID | Name |" in text
    assert "| WO-01 | Governance |" in text
    assert meta.format == "docx"
    assert meta.tables_count == 1
    assert "Architecture Overview" in meta.headings


def test_parse_image():
    png_bytes = _create_mock_png(width=800, height=450)
    text_desc, meta = parse_image(png_bytes, "dashboard.png")
    assert meta.width == 800
    assert meta.height == 450
    assert meta.aspect_ratio == "16:9"
    assert meta.format.lower() == "png"
    assert "800x450" in text_desc


def test_safe_extract_archive_with_zip_slip_protection(tmp_path: Path):
    target_extract = tmp_path / "extracted"

    # Create zip with valid files and a malicious directory traversal attempt
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/spec.md", "# Specification\nMust implement Pillar 4.")
        zf.writestr("images/logo.txt", "mock image text")
        # Malicious Zip Slip entry
        zf.writestr("../../evil.txt", "Attacker payload")
        zf.writestr("../another_evil.txt", "Attacker payload 2")

    zip_bytes = buf.getvalue()
    summary, meta, extracted_paths = safe_extract_archive(zip_bytes, "project.zip", target_extract)

    assert meta.total_files == 2
    assert (target_extract / "docs" / "spec.md").is_file()
    assert (target_extract / "images" / "logo.txt").is_file()
    # Confirm malicious files were completely blocked from escaping target_extract
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "another_evil.txt").exists()
    assert "Total Files:** 2" in summary


def test_attachment_store_lifecycle(tmp_path: Path):
    store = AttachmentStore(tmp_path)
    assert store.list_attachments() == []

    # 1. Ingest document
    doc_path = tmp_path / "resume.md"
    doc_path.write_text("# John Doe Resume\n\nExperience with Python and Distributed Systems.", encoding="utf-8")
    rec1 = store.ingest(doc_path, tags=["user_input", "resume"])
    assert rec1.attachment_id.startswith("ATT-")
    assert rec1.attachment_type == AttachmentType.DOCUMENT
    assert "John Doe Resume" in rec1.text_content
    assert (tmp_path / rec1.raw_relpath).is_file()
    assert (tmp_path / rec1.extracted_relpath).is_file()

    # 2. Ingest image in memory
    png_bytes = _create_mock_png(width=400, height=300)
    rec2 = store.ingest_bytes(png_bytes, filename="mock_design.png", tags=["mockup"])
    assert rec2.attachment_type == AttachmentType.IMAGE
    assert rec2.metadata["width"] == 400

    # 3. Deduplication check
    rec1_dup = store.ingest(doc_path)
    assert rec1_dup.attachment_id == rec1.attachment_id
    assert len(store.list_attachments()) == 2

    # 4. Lookup
    found = store.get_attachment("resume.md")
    assert found is not None
    assert found.attachment_id == rec1.attachment_id

    # 5. Build grounding context
    context_str = store.build_grounding_context(token_budget=1000)
    assert "## VERIFIED ATTACHMENTS & GROUNDING ASSETS" in context_str
    assert "resume.md" in context_str
    assert "John Doe Resume" in context_str
    assert "mock_design.png" in context_str

    # 6. Delete attachment
    deleted = store.delete_attachment(rec1.attachment_id)
    assert deleted
    assert len(store.list_attachments()) == 1
    assert not (tmp_path / rec1.raw_relpath).exists()


def test_tui_attach_commands_and_dispatch(tmp_path: Path):
    from click.testing import CliRunner
    from cli.main import cli
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState

    class DummyClient:
        pass

    class DummyAdapter:
        pass

    # Setup file
    sample_file = tmp_path / "spec.txt"
    sample_file.write_text("API endpoints for billing service", encoding="utf-8")

    session = {"session_id": "test-session", "workspace": str(tmp_path)}
    state = AutonomousDeliveryState(session_id="test-session")

    # 1. Dispatch :attach
    sess, should_exit = dispatch_delivery_command(
        DummyAdapter(), DummyClient(), session, f":attach {sample_file}", state
    )
    assert not should_exit
    assert state.messages[-2].content.startswith(":attach")
    assert "ATTACHMENT INGESTED" in state.messages[-1].content
    assert "spec.txt" in state.messages[-1].content

    # 2. Dispatch :attachments
    sess, should_exit = dispatch_delivery_command(
        DummyAdapter(), DummyClient(), session, ":attachments", state
    )
    assert not should_exit
    assert "WORKSPACE ATTACHMENTS" in state.messages[-1].content
    assert "spec.txt" in state.messages[-1].content

    # 3. Test CLI command stackmind attach and stackmind attachments
    runner = CliRunner()
    sample_img = tmp_path / "diagram.png"
    sample_img.write_bytes(_create_mock_png(200, 200))
    res = runner.invoke(cli, ["attach", str(sample_img), "--workspace", str(tmp_path)])
    assert res.exit_code == 0
    assert "ATTACHMENT INGESTED" in res.output

    res2 = runner.invoke(cli, ["attachments", "--workspace", str(tmp_path)])
    assert res2.exit_code == 0
    assert "diagram.png" in res2.output

