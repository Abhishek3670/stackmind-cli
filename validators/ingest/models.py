"""Models and data structures for StackMind Multi-Modal Attachment & Ingestion Engine (Pillar 4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AttachmentType(str, Enum):
    """Broad category of ingested user/system attachment."""

    DOCUMENT = "document"
    IMAGE = "image"
    ARCHIVE = "archive"
    CODE = "code"
    DATA = "data"
    OTHER = "other"


@dataclass
class DocumentMetadata:
    """Metadata specific to document files (PDF, DOCX, TXT, MD, HTML, CSV)."""

    format: str
    word_count: int = 0
    character_count: int = 0
    page_count: int = 1
    tables_count: int = 0
    headings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentMetadata:
        return cls(
            format=data.get("format", "text"),
            word_count=data.get("word_count", 0),
            character_count=data.get("character_count", 0),
            page_count=data.get("page_count", 1),
            tables_count=data.get("tables_count", 0),
            headings=list(data.get("headings", [])),
        )


@dataclass
class ImageMetadata:
    """Metadata specific to raster and vector images."""

    format: str
    width: int
    height: int
    aspect_ratio: str
    color_mode: str = "RGB"
    file_size_bytes: int = 0
    alt_description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageMetadata:
        return cls(
            format=data.get("format", "unknown"),
            width=data.get("width", 0),
            height=data.get("height", 0),
            aspect_ratio=data.get("aspect_ratio", "1:1"),
            color_mode=data.get("color_mode", "RGB"),
            file_size_bytes=data.get("file_size_bytes", 0),
            alt_description=data.get("alt_description", ""),
        )


@dataclass
class ArchiveMetadata:
    """Metadata specific to compressed archives (.zip, .tar, .tgz)."""

    format: str
    total_files: int = 0
    uncompressed_size_bytes: int = 0
    compressed_size_bytes: int = 0
    extracted_members: list[str] = field(default_factory=list)
    document_count: int = 0
    image_count: int = 0
    code_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveMetadata:
        return cls(
            format=data.get("format", "zip"),
            total_files=data.get("total_files", 0),
            uncompressed_size_bytes=data.get("uncompressed_size_bytes", 0),
            compressed_size_bytes=data.get("compressed_size_bytes", 0),
            extracted_members=list(data.get("extracted_members", [])),
            document_count=data.get("document_count", 0),
            image_count=data.get("image_count", 0),
            code_count=data.get("code_count", 0),
        )


@dataclass
class AttachmentRecord:
    """Top-level record tracking an ingested attachment."""

    attachment_id: str
    original_filename: str
    attachment_type: AttachmentType
    sha256: str
    size_bytes: int
    created_at: str
    raw_relpath: str
    extracted_relpath: str | None = None
    summary: str = ""
    text_content: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attachment_type"] = self.attachment_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttachmentRecord:
        raw_type = data.get("attachment_type", AttachmentType.OTHER.value)
        try:
            att_type = AttachmentType(raw_type)
        except ValueError:
            att_type = AttachmentType.OTHER

        return cls(
            attachment_id=data["attachment_id"],
            original_filename=data["original_filename"],
            attachment_type=att_type,
            sha256=data["sha256"],
            size_bytes=data.get("size_bytes", 0),
            created_at=data.get("created_at", ""),
            raw_relpath=data.get("raw_relpath", ""),
            extracted_relpath=data.get("extracted_relpath"),
            summary=data.get("summary", ""),
            text_content=data.get("text_content", ""),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
        )
