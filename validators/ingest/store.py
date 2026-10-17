"""AttachmentStore: Local repository persistence and grounding context engine for Pillar 4 attachments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AttachmentRecord, AttachmentType
from .parsers import (
    detect_attachment_type,
    parse_document,
    parse_image,
    safe_extract_archive,
)


class AttachmentStore:
    """Manages workspace attachments under `<workspace>/.sync/attachments/`."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.attachments_dir = self.project_path / ".sync" / "attachments"
        self.raw_dir = self.attachments_dir / "raw"
        self.extracted_dir = self.attachments_dir / "extracted"
        self.manifest_path = self.attachments_dir / "manifest.json"

        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)

    def _load_manifest(self) -> list[AttachmentRecord]:
        if not self.manifest_path.is_file():
            return []
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [AttachmentRecord.from_dict(item) for item in data]
        except Exception:
            return []

    def _save_manifest(self, records: list[AttachmentRecord]) -> None:
        raw_list = [r.to_dict() for r in records]
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(raw_list, f, indent=2, ensure_ascii=False)

    def list_attachments(self) -> list[AttachmentRecord]:
        """List all tracked attachments."""
        return self._load_manifest()

    def get_attachment(self, identifier: str) -> AttachmentRecord | None:
        """Find an attachment by ID, hash, or filename."""
        clean = identifier.strip().lower()
        for rec in self.list_attachments():
            if (
                rec.attachment_id.lower() == clean
                or rec.sha256.lower().startswith(clean)
                or rec.original_filename.lower() == clean
            ):
                return rec
        return None

    def ingest(
        self,
        source_path: Path | str,
        tags: list[str] | None = None,
        description: str | None = None,
    ) -> AttachmentRecord:
        """Ingest a file from disk into the store."""
        src = Path(source_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Attachment file not found: {source_path}")

        raw_bytes = src.read_bytes()
        return self.ingest_bytes(
            raw_bytes,
            filename=src.name,
            tags=tags,
            description=description,
        )

    def ingest_bytes(
        self,
        raw_bytes: bytes,
        filename: str,
        tags: list[str] | None = None,
        description: str | None = None,
    ) -> AttachmentRecord:
        """Ingest raw bytes into the attachment store."""
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        hash_prefix = sha256[:12]
        att_id = f"ATT-{hash_prefix}"

        # Deduplication check
        existing = self.get_attachment(att_id)
        if existing is not None:
            return existing

        created_at = datetime.now(timezone.utc).isoformat()
        att_type = detect_attachment_type(filename, raw_bytes[:16])

        # 1. Save raw immutable file
        raw_target_dir = self.raw_dir / hash_prefix
        raw_target_dir.mkdir(parents=True, exist_ok=True)
        clean_filename = Path(filename).name
        raw_file = raw_target_dir / clean_filename
        raw_file.write_bytes(raw_bytes)
        raw_relpath = str(raw_file.relative_to(self.project_path)).replace("\\", "/")

        # 2. Extract contents based on type
        extracted_relpath: str | None = None
        extracted_text = ""
        meta_dict: dict[str, Any] = {}
        summary = description or ""

        target_extract_dir = self.extracted_dir / hash_prefix
        target_extract_dir.mkdir(parents=True, exist_ok=True)

        if att_type in (AttachmentType.DOCUMENT, AttachmentType.DATA, AttachmentType.CODE):
            text, doc_meta = parse_document(raw_bytes, clean_filename)
            meta_dict = doc_meta.to_dict()
            extracted_text = text
            out_md = target_extract_dir / f"{clean_filename}.md"
            out_md.write_text(text, encoding="utf-8")
            extracted_relpath = str(out_md.relative_to(self.project_path)).replace("\\", "/")
            if not summary:
                summary = f"{doc_meta.format.upper()} document ({doc_meta.word_count} words, {doc_meta.page_count} pages)"

        elif att_type == AttachmentType.IMAGE:
            text, img_meta = parse_image(raw_bytes, clean_filename)
            meta_dict = img_meta.to_dict()
            extracted_text = text
            out_md = target_extract_dir / "image_info.md"
            out_md.write_text(text, encoding="utf-8")
            extracted_relpath = str(out_md.relative_to(self.project_path)).replace("\\", "/")
            if not summary:
                summary = f"{img_meta.width}x{img_meta.height} {img_meta.format.upper()} image ({img_meta.aspect_ratio})"

        elif att_type == AttachmentType.ARCHIVE:
            unpacked_dir = target_extract_dir / "unpacked"
            summary_text, arc_meta, _ = safe_extract_archive(raw_bytes, clean_filename, unpacked_dir)
            meta_dict = arc_meta.to_dict()
            extracted_text = summary_text
            out_md = target_extract_dir / "archive_manifest.md"
            out_md.write_text(summary_text, encoding="utf-8")
            extracted_relpath = str(unpacked_dir.relative_to(self.project_path)).replace("\\", "/")
            if not summary:
                summary = f"Archive containing {arc_meta.total_files} extracted files ({arc_meta.format.upper()})"

        else:
            # Fallback text decoding
            try:
                extracted_text = raw_bytes.decode("utf-8")
            except Exception:
                extracted_text = f"Binary attachment of type {att_type.value}"
            summary = summary or f"Attachment: {clean_filename}"

        record = AttachmentRecord(
            attachment_id=att_id,
            original_filename=clean_filename,
            attachment_type=att_type,
            sha256=sha256,
            size_bytes=len(raw_bytes),
            created_at=created_at,
            raw_relpath=raw_relpath,
            extracted_relpath=extracted_relpath,
            summary=summary,
            text_content=extracted_text,
            tags=list(tags or []),
            metadata=meta_dict,
        )

        records = self._load_manifest()
        records.append(record)
        self._save_manifest(records)
        return record

    def delete_attachment(self, identifier: str) -> bool:
        """Remove an attachment and its files."""
        records = self._load_manifest()
        target: AttachmentRecord | None = None
        remaining: list[AttachmentRecord] = []

        clean = identifier.strip().lower()
        for r in records:
            if (
                r.attachment_id.lower() == clean
                or r.sha256.lower().startswith(clean)
                or r.original_filename.lower() == clean
            ):
                target = r
            else:
                remaining.append(r)

        if target is None:
            return False

        # Cleanup disk files
        hash_prefix = target.sha256[:12]
        raw_p = self.raw_dir / hash_prefix
        if raw_p.exists():
            shutil.rmtree(raw_p, ignore_errors=True)
        ext_p = self.extracted_dir / hash_prefix
        if ext_p.exists():
            shutil.rmtree(ext_p, ignore_errors=True)

        self._save_manifest(remaining)
        return True

    def build_grounding_context(
        self,
        query: str | None = None,
        token_budget: int = 2500,
    ) -> str:
        """Synthesize a prompt-ready Markdown grounding section for agent turns."""
        attachments = self.list_attachments()
        if not attachments:
            return ""

        lines: list[str] = [
            "## VERIFIED ATTACHMENTS & GROUNDING ASSETS",
            "The following user attachments have been uploaded to `.sync/attachments/`.",
            "You MUST ground your planning, code, and deliverables on these verified contents:",
            "",
        ]

        char_budget = token_budget * 4
        current_chars = 0

        for att in attachments:
            header = f"### [{att.attachment_id}] {att.original_filename} ({att.attachment_type.value.upper()})"
            lines.append(header)
            lines.append(f"- **Summary:** {att.summary}")
            if att.extracted_relpath:
                lines.append(f"- **Location:** `{att.extracted_relpath}`")

            # Include content snippet
            if att.text_content:
                snippet = att.text_content.strip()
                # Limit each attachment snippet
                if len(snippet) > 1500:
                    snippet = snippet[:1500] + "\n...(snippet truncated for length)..."
                lines.append("\n```text")
                lines.append(snippet)
                lines.append("```\n")

            current_chars = sum(len(l) for l in lines)
            if current_chars >= char_budget:
                lines.append("*(Remaining attachments omitted to fit token budget)*")
                break

        return "\n".join(lines)
