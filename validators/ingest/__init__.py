"""StackMind Multi-Modal Attachment & Ingestion Subsystem (Pillar 4)."""

from .models import (
    ArchiveMetadata,
    AttachmentRecord,
    AttachmentType,
    DocumentMetadata,
    ImageMetadata,
)
from .parsers import (
    detect_attachment_type,
    parse_document,
    parse_image,
    safe_extract_archive,
)
from .store import AttachmentStore

__all__ = [
    "ArchiveMetadata",
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentType",
    "DocumentMetadata",
    "ImageMetadata",
    "detect_attachment_type",
    "parse_document",
    "parse_image",
    "safe_extract_archive",
]
