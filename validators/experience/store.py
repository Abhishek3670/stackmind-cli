"""Filesystem storage, loading, and querying for raw Experience Records.

Maintains Tier 1 immutable experience artifacts under .sync/experience/records/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from validators.experience.models import ExperienceRecord
from validators.harness.snapshot import TrustLevel


class ExperienceStore:
    """Authoritative filesystem storage manager for Experience Records."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.experience_dir = self.project_path / ".sync" / "experience"
        self.records_dir = self.experience_dir / "records"

    def ensure_directories(self) -> None:
        """Ensure .sync/experience/records directory exists."""
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def save_record(self, record: ExperienceRecord, *, validate_schema: bool = True) -> Path:
        """Atomically persist an experience record as an immutable JSON artifact."""
        self.ensure_directories()
        if validate_schema:
            schema_path = self.project_path / "schemas" / "experience.schema.json"
            record.validate_schema(schema_path if schema_path.exists() else None)

        target_file = self.records_dir / f"{record.experience_id}.json"
        tmp_file = self.records_dir / f"{record.experience_id}.tmp"

        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True)
        tmp_file.write_text(payload, encoding="utf-8")
        os.replace(tmp_file, target_file)
        return target_file

    def load_record(self, experience_id: str) -> ExperienceRecord | None:
        """Load and deserialize an experience record by ID."""
        target_file = self.records_dir / f"{experience_id}.json"
        if not target_file.exists():
            return None
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            return ExperienceRecord.from_dict(data)
        except Exception:
            return None

    get_record = load_record

    def list_records(
        self,
        *,
        only_learning_eligible: bool = False,
        agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[ExperienceRecord]:
        """List and filter stored experience records sorted newest to oldest."""
        if not self.records_dir.exists():
            return []

        records: list[ExperienceRecord] = []
        for path in self.records_dir.glob("EXP-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rec = ExperienceRecord.from_dict(data)
                if only_learning_eligible and not rec.learning_eligible:
                    continue
                if agent_id and rec.agent_id != agent_id:
                    continue
                records.append(rec)
            except Exception:
                continue

        records.sort(key=lambda r: r.recorded_at, reverse=True)
        if limit:
            return records[:limit]
        return records

    def stats(self) -> dict[str, int]:
        """Compute aggregate statistics of captured experience records."""
        records = self.list_records()
        return {
            "learning_eligible": sum(1 for r in records if r.learning_eligible),
            "observable": sum(1 for r in records if r.trust_level == TrustLevel.OBSERVABLE),
            "total": len(records),
            "verified": sum(1 for r in records if r.trust_level == TrustLevel.VERIFIED),
        }
