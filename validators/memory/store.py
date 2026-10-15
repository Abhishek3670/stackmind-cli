"""Filesystem storage, loading, and querying for Durable Memory Records.

Maintains Tier 1 immutable memory artifacts under .sync/memory/records/.
Derived SQLite/FTS5 index under .sync/memory/cache/memory_index.db.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from validators.memory.models import (
    MemoryEvidence,
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)
from validators.memory.promotion import MemoryPromotionPolicy, MemoryPromotionService, PromotionDecision
from validators.memory.contradiction import ContradictionDetector, ContradictionSeverity, ContradictionStatus
from validators.experience.store import ExperienceStore
from validators.experience.index import ExperienceIndex
from validators.experience.models import ExperienceRecord


@dataclass(frozen=True)
class MemorySearchResult:
    """A ranked search hit from the memory full-text compilation index."""

    memory_id: str
    memory_type: MemoryType
    content: str
    project_id: str
    status: MemoryStatus
    provenance_confidence: float
    created_at: str
    rank: float
    matched_snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "created_at": self.created_at,
            "matched_snippet": self.matched_snippet,
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.value,
            "project_id": self.project_id,
            "provenance_confidence": self.provenance_confidence,
            "rank": self.rank,
            "status": self.status.value,
        }


class MemoryStore:
    """Authoritative filesystem storage manager for Memory Records."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.memory_dir = self.project_path / ".sync" / "memory"
        self.records_dir = self.memory_dir / "records"
        self.cache_dir = self.memory_dir / "cache"
        self.db_path = self.cache_dir / "memory_index.db"
        self.exp_store = ExperienceStore(self.project_path)

    def ensure_directories(self) -> None:
        """Ensure .sync/memory/records and cache directories exist."""
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def save_record(self, record: MemoryRecord, *, validate_schema: bool = True) -> Path:
        """Atomically persist a memory record as an immutable JSON artifact."""
        self.ensure_directories()
        if validate_schema:
            schema_path = self.project_path / "schemas" / "memory.schema.json"
            record.validate_schema(schema_path if schema_path.exists() else None)

        target_file = self.records_dir / f"{record.memory_id}.json"
        tmp_file = self.records_dir / f"{record.memory_id}.tmp"

        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True)
        tmp_file.write_text(payload, encoding="utf-8")
        os.replace(tmp_file, target_file)

        # Update derived index
        self._update_index_for_record(record)
        return target_file

    def load_record(self, memory_id: str) -> MemoryRecord | None:
        """Load and deserialize a memory record by ID."""
        target_file = self.records_dir / f"{memory_id}.json"
        if not target_file.exists():
            return None
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            return MemoryRecord.from_dict(data)
        except Exception:
            return None

    get_record = load_record

    def list_records(
        self,
        *,
        project_id: str | None = None,
        status: MemoryStatus | None = None,
        memory_type: MemoryType | None = None,
        limit: int | None = None,
        include_expired: bool = False,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        """List and filter stored memory records sorted newest to oldest.

        By default, excludes EXPIRED and DELETED memories for retention consistency.
        Also excludes memories that have expired based on expires_at timestamp.
        """
        if not self.records_dir.exists():
            return []

        records: list[MemoryRecord] = []
        for path in self.records_dir.glob("MEM-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rec = MemoryRecord.from_dict(data)
                if project_id and rec.project_id != project_id:
                    continue
                if status and rec.status != status:
                    continue
                if memory_type and rec.memory_type != memory_type:
                    continue
                if not include_expired and rec.status == MemoryStatus.EXPIRED:
                    continue
                if not include_deleted and rec.status == MemoryStatus.DELETED:
                    continue
                # Also check expiration timestamp for all memories
                if not include_expired and rec.expires_at is not None and self._is_expired_record(rec):
                    continue
                records.append(rec)
            except Exception:
                continue

        records.sort(key=lambda r: r.updated_at, reverse=True)
        if limit:
            return records[:limit]
        return records

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
        limit: int = 10,
        include_expired: bool = False,
        include_deleted: bool = False,
    ) -> list[MemorySearchResult]:
        """Full-text BM25 search over compiled memory records.

        By default, excludes EXPIRED and DELETED memories for retention consistency.
        Also excludes memories that have expired based on expires_at timestamp.
        """
        if not self.db_path.exists():
            self.build_index()

        conn = self._get_connection()
        sanitized_query = query.replace('"', '""').strip()
        if not sanitized_query:
            return []

        tokens = [f'"{t}"' for t in sanitized_query.split() if t.isalnum() or "-" in t or "_" in t]
        if not tokens:
            tokens = [f'"{sanitized_query}"']
        match_expr = " OR ".join(tokens)

        filters = []
        params: list[Any] = [match_expr]

        if project_id:
            filters.append("m.project_id = ?")
            params.append(project_id)
        if status:
            filters.append("m.status = ?")
            params.append(status.value)
        if not include_expired:
            filters.append("m.status != ?")
            params.append(MemoryStatus.EXPIRED.value)
            # Also exclude ACTIVE memories that have expired based on expires_at
            filters.append("(m.status != ? OR m.expires_at IS NULL OR m.expires_at > datetime('now'))")
            params.append(MemoryStatus.ACTIVE.value)
        if not include_deleted:
            filters.append("m.status != ?")
            params.append(MemoryStatus.DELETED.value)

        where_clause = f"AND {' AND '.join(filters)}" if filters else ""
        params.append(limit)

        sql = f"""
            SELECT
                m.memory_id,
                m.memory_type,
                m.content,
                m.project_id,
                m.status,
                m.provenance_confidence,
                m.created_at,
                bm25(memory_fts) AS rank,
                snippet(memory_fts, 3, '[match]', '[/match]', '...', 16) AS matched_snippet
            FROM memory_fts f
            JOIN memory_entries m ON f.memory_id = m.memory_id
            WHERE memory_fts MATCH ?
            {where_clause}
            ORDER BY rank ASC, m.created_at DESC
            LIMIT ?;
        """

        results: list[MemorySearchResult] = []
        try:
            cursor = conn.execute(sql, params)
            for row in cursor.fetchall():
                results.append(
                    MemorySearchResult(
                        memory_id=row["memory_id"],
                        memory_type=MemoryType(row["memory_type"]),
                        content=row["content"],
                        project_id=row["project_id"],
                        status=MemoryStatus(row["status"]),
                        provenance_confidence=float(row["provenance_confidence"]),
                        created_at=row["created_at"],
                        rank=float(row["rank"]),
                        matched_snippet=row["matched_snippet"],
                    )
                )
        except sqlite3.OperationalError:
            # Fallback to direct SQL LIKE query
            sql_fallback = f"""
                SELECT
                    m.memory_id, m.memory_type, m.content, m.project_id,
                    m.status, m.provenance_confidence, m.created_at,
                    0.0 as rank, NULL as matched_snippet
                FROM memory_entries m
                WHERE (m.content LIKE ? OR m.normalized_content LIKE ?)
                {where_clause}
                ORDER BY m.created_at DESC
                LIMIT ?;
            """
            like_param = f"%{sanitized_query}%"
            fallback_params = [like_param, like_param] + params[1:]
            cursor = conn.execute(sql_fallback, fallback_params)
            for row in cursor.fetchall():
                results.append(
                    MemorySearchResult(
                        memory_id=row["memory_id"],
                        memory_type=MemoryType(row["memory_type"]),
                        content=row["content"],
                        project_id=row["project_id"],
                        status=MemoryStatus(row["status"]),
                        provenance_confidence=float(row["provenance_confidence"]),
                        created_at=row["created_at"],
                        rank=0.0,
                        matched_snippet=None,
                    )
                )
        finally:
            conn.close()

        return results

    def stats(self) -> dict[str, Any]:
        """Return memory index statistics."""
        records = self.list_records()
        return {
            "total": len(records),
            "active": sum(1 for r in records if r.status == MemoryStatus.ACTIVE),
            "candidate": sum(1 for r in records if r.status == MemoryStatus.CANDIDATE),
            "superseded": sum(1 for r in records if r.status == MemoryStatus.SUPERSEDED),
            "expired": sum(1 for r in records if r.status == MemoryStatus.EXPIRED),
            "deleted": sum(1 for r in records if r.status == MemoryStatus.DELETED),
            "by_type": {
                t.value: sum(1 for r in records if r.memory_type == t)
                for t in MemoryType
            },
        }

    def _get_connection(self) -> sqlite3.Connection:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """Initialize relational and FTS5 tables."""
        should_close = False
        if conn is None:
            conn = self._get_connection()
            should_close = True

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_entries (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                provenance_confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_validated_at TEXT,
                expires_at TEXT,
                superseded_by TEXT,
                supersedes TEXT,  -- JSON array
                tags TEXT,  -- JSON array
                record_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mem_project ON memory_entries(project_id);
            CREATE INDEX IF NOT EXISTS idx_mem_status ON memory_entries(status);
            CREATE INDEX IF NOT EXISTS idx_mem_type ON memory_entries(memory_type);
            CREATE INDEX IF NOT EXISTS idx_mem_created ON memory_entries(created_at);

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                memory_id UNINDEXED,
                content,
                normalized_content,
                tags_text
            );
            """
        )

        if should_close:
            conn.close()

    def build_index(self, *, clean: bool = False) -> int:
        """Rebuild or initialize the index completely from Tier 1 raw records.

        Excludes EXPIRED and DELETED memories to prevent resurrection.
        """
        if clean and self.db_path.exists():
            try:
                self.db_path.unlink()
            except OSError:
                pass

        conn = self._get_connection()
        self.init_schema(conn)

        # Use include_expired=False, include_deleted=False (defaults) to exclude expired/deleted
        records = self.list_records()
        with conn:
            conn.execute("DELETE FROM memory_fts;")
            conn.execute("DELETE FROM memory_entries;")

            for record in records:
                self._insert_record(conn, record)

        indexed_count = len(records)
        conn.close()
        return indexed_count

    def _update_index_for_record(self, record: MemoryRecord) -> None:
        """Incrementally update index for a single record."""
        conn = self._get_connection()
        self.init_schema(conn)

        raw_bytes = json.dumps(record.to_dict(), sort_keys=True).encode("utf-8")
        record_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Check if already indexed with same hash
        cursor = conn.execute("SELECT record_hash FROM memory_entries WHERE memory_id = ?;", (record.memory_id,))
        row = cursor.fetchone()
        if row and row["record_hash"] == record_hash:
            conn.close()
            return

        with conn:
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?;", (record.memory_id,))
            conn.execute("DELETE FROM memory_entries WHERE memory_id = ?;", (record.memory_id,))
            self._insert_record(conn, record, record_hash=record_hash)

        conn.close()

    def _insert_record(
        self,
        conn: sqlite3.Connection,
        record: MemoryRecord,
        *,
        record_hash: str | None = None,
    ) -> None:
        raw_dict = record.to_dict()
        if record_hash is None:
            raw_bytes = json.dumps(raw_dict, sort_keys=True).encode("utf-8")
            record_hash = hashlib.sha256(raw_bytes).hexdigest()

        tags_text = " ".join(record.tags)

        cursor = conn.execute(
            """
            INSERT INTO memory_entries (
                memory_id, memory_type, content, normalized_content,
                project_id, status, provenance_confidence,
                created_at, updated_at, last_validated_at,
                expires_at, superseded_by, supersedes, tags,
                record_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record.memory_id,
                record.memory_type.value,
                record.content,
                record.normalized_content,
                record.project_id,
                record.status.value,
                record.provenance.confidence,
                record.created_at,
                record.updated_at,
                record.last_validated_at,
                record.expires_at,
                record.superseded_by,
                json.dumps(list(record.supersedes)),
                json.dumps(list(record.tags)),
                record_hash,
                json.dumps(raw_dict, sort_keys=True),
            ),
        )

        rowid = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO memory_fts (
                rowid, memory_id, content, normalized_content, tags_text
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (
                rowid,
                record.memory_id,
                record.content,
                record.normalized_content,
                tags_text,
            ),
        )


def normalize_content(content: str) -> str:
    """Normalize memory content for deterministic deduplication.

    Handles:
    - Unicode normalization (NFC)
    - Whitespace normalization (collapse repeated, strip)
    - Case normalization for non-identifier content
    - Empty content handling

    Preserves meaningful code/identifier differences.
    """
    if not content:
        return ""

    # Unicode normalization
    normalized = unicodedata.normalize("NFC", content)

    # Whitespace normalization
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip()

    # Don't aggressively lowercase - preserve meaningful case differences in identifiers
    # But normalize common words to lowercase for better matching
    # This is a conservative approach - could be made more sophisticated later

    return normalized


def make_uniqueness_key(normalized_content: str, project_id: str) -> str:
    """Create a logical uniqueness key from normalized content and project."""
    return f"{project_id}:{normalized_content}"


class MemoryCompiler:
    """Extracts candidate memories from verified experience records.

    Implements the memory lifecycle: experience -> candidate -> validation -> active.
    """

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.memory_store = MemoryStore(self.project_path)
        self.exp_store = ExperienceStore(self.project_path)
        self.exp_index = ExperienceIndex(self.project_path)

    def compile_memories_from_experience(
        self,
        experience: ExperienceRecord,
        *,
        extractor_agent: str = "memory-compiler",
    ) -> list[MemoryRecord]:
        """Analyze an experience record and extract candidate memories.

        This is the core memory extraction logic that evaluates:
        - Trust level and verification dimensions
        - Outcome quality
        - Presence of reusable facts/decisions/lessons
        - Project scope and sensitivity
        """
        candidates: list[MemoryRecord] = []

        # Gate: only learning-eligible, completed experiences
        if not experience.learning_eligible or experience.outcome != "completed":
            return candidates

        # Extract facts from verification outcomes
        fact_candidates = self._extract_facts(experience)
        candidates.extend(fact_candidates)

        # Extract decisions from work order outcomes
        decision_candidates = self._extract_decisions(experience)
        candidates.extend(decision_candidates)

        # Extract lessons from failures and corrections
        lesson_candidates = self._extract_lessons(experience)
        candidates.extend(lesson_candidates)

        # Filter for project scope and duplicates
        filtered = self._filter_candidates(candidates, experience)

        return filtered

    def _extract_facts(self, experience: ExperienceRecord) -> list[MemoryRecord]:
        """Extract verified factual claims from the experience."""
        candidates: list[MemoryRecord] = []
        dim = experience.verification.dimensions

        # Fact: code change verification passed
        if dim.code_verified and experience.verification.observed_diff.all_changed_files:
            files = ", ".join(experience.verification.observed_diff.all_changed_files)
            content = f"Verified code changes to: {files}"
            normalized = f"verified code changes {files}"

            evidence = MemoryEvidence(
                experience_id=experience.experience_id,
                evidence_type="verification_dimension",
                excerpt=f"code_verified=True, modified_files={files}",
                dimension_scores={"code_verified": True},
            )

            try:
                mem = MemoryRecord.from_experience(
                    project_id=experience.work_order_id or experience.task_id,
                    experience=experience,
                    memory_type=MemoryType.FACT,
                    content=content,
                    normalized_content=normalized,
                    evidence=(evidence,),
                )
                candidates.append(mem)
            except ValueError:
                pass

        # Fact: test verification passed
        if dim.behavioral_verified and experience.verification.tests_passed:
            content = f"Tests passed for work order {experience.work_order_id or experience.task_id}"
            normalized = f"tests passed {experience.work_order_id or experience.task_id}"

            evidence = MemoryEvidence(
                experience_id=experience.experience_id,
                evidence_type="verification_dimension",
                excerpt="behavioral_verified=True, tests_passed=True",
                dimension_scores={"behavioral_verified": True, "outcome_verified": True},
            )

            try:
                mem = MemoryRecord.from_experience(
                    project_id=experience.work_order_id or experience.task_id,
                    experience=experience,
                    memory_type=MemoryType.FACT,
                    content=content,
                    normalized_content=normalized,
                    evidence=(evidence,),
                )
                candidates.append(mem)
            except ValueError:
                pass

        return candidates

    def _extract_decisions(self, experience: ExperienceRecord) -> list[MemoryRecord]:
        """Extract architectural/design decisions from the experience."""
        candidates: list[MemoryRecord] = []

        # Decision: work order was approved and completed
        if experience.work_order_id and experience.outcome == "completed":
            summary = experience.final_state.get("summary", "")
            content = f"Approved work order {experience.work_order_id}: {summary}"
            normalized = f"approved work order {experience.work_order_id} {summary}"

            evidence = MemoryEvidence(
                experience_id=experience.experience_id,
                evidence_type="final_state",
                excerpt=f"outcome=completed, work_order_id={experience.work_order_id}, summary={summary}",
                dimension_scores={"outcome_verified": experience.verification.dimensions.outcome_verified},
            )

            try:
                mem = MemoryRecord.from_experience(
                    project_id=experience.work_order_id,
                    experience=experience,
                    memory_type=MemoryType.DECISION,
                    content=content,
                    normalized_content=normalized,
                    evidence=(evidence,),
                )
                candidates.append(mem)
            except ValueError:
                pass

        return candidates

    def _extract_lessons(self, experience: ExperienceRecord) -> list[MemoryRecord]:
        """Extract lessons from failures and corrections."""
        candidates: list[MemoryRecord] = []

        for failure in experience.failures:
            # Find matching correction
            correction = None
            for corr in experience.corrections:
                if corr.failure_ref == failure.phase or corr.failure_ref in failure.error_message:
                    correction = corr
                    break

            if correction:
                content = f"When {failure.phase} fails with '{failure.error_message}', fix: {correction.correction_action} ({correction.rationale})"
                normalized = f"failure {failure.phase} fix {correction.correction_action}"

                evidence = MemoryEvidence(
                    experience_id=experience.experience_id,
                    evidence_type="correction",
                    excerpt=f"failure: {failure.error_message}, correction: {correction.correction_action}, rationale: {correction.rationale}",
                    dimension_scores={},
                )

                try:
                    mem = MemoryRecord.from_experience(
                        project_id=experience.work_order_id or experience.task_id,
                        experience=experience,
                        memory_type=MemoryType.LESSON,
                        content=content,
                        normalized_content=normalized,
                        evidence=(evidence,),
                    )
                    candidates.append(mem)
                except ValueError:
                    pass
            else:
                # Unresolved failure - record as lesson about what doesn't work
                content = f"Approach failed: {failure.phase} with '{failure.error_message}' - no resolution found"
                normalized = f"failed approach {failure.phase} {failure.error_message}"

                evidence = MemoryEvidence(
                    experience_id=experience.experience_id,
                    evidence_type="failure",
                    excerpt=f"unresolved failure: {failure.error_message}",
                    dimension_scores={},
                )

                try:
                    mem = MemoryRecord.from_experience(
                        project_id=experience.work_order_id or experience.task_id,
                        experience=experience,
                        memory_type=MemoryType.LESSON,
                        content=content,
                        normalized_content=normalized,
                        evidence=(evidence,),
                        confidence=0.4,  # Lower confidence for unresolved failures
                    )
                    candidates.append(mem)
                except ValueError:
                    pass

        return candidates

    def _filter_candidates(
        self,
        candidates: list[MemoryRecord],
        source_experience: ExperienceRecord,
    ) -> list[MemoryRecord]:
        """Filter candidates for duplicates, scope, and quality.

        Implements idempotent compile-time deduplication:
        - Normalizes candidate content
        - Checks for existing matching memory within project boundary
        - Updates provenance on existing memory rather than creating duplicate
        - Preserves historical evidence and existing memory IDs
        - Does NOT match EXPIRED or DELETED memories
        - Runs contradiction detection against existing active memories
        """
        filtered: list[MemoryRecord] = []

        # Initialize contradiction detector for this project
        detector = ContradictionDetector(self.project_path, memory_store=self.memory_store)

        for candidate in candidates:
            # Normalize candidate content for deduplication
            normalized = normalize_content(candidate.normalized_content)
            uniqueness_key = make_uniqueness_key(normalized, candidate.project_id)

            # Check for existing ACTIVE memory with same uniqueness key
            # Excludes EXPIRED and DELETED for deduplication
            existing_records = self.memory_store.list_records(
                project_id=candidate.project_id,
                status=MemoryStatus.ACTIVE,
                include_expired=False,
                include_deleted=False,
            )

            matched_existing = None
            for existing in existing_records:
                existing_normalized = normalize_content(existing.normalized_content)
                existing_key = make_uniqueness_key(existing_normalized, existing.project_id)
                if existing_key == uniqueness_key:
                    matched_existing = existing
                    break

            if matched_existing:
                # Deduplicated: update provenance on existing memory instead of creating duplicate
                # This preserves the existing memory ID and historical evidence
                self._update_memory_provenance(matched_existing, candidate)
                # Still return the candidate info so caller knows it was deduplicated
                candidate = MemoryRecord(
                    memory_id=matched_existing.memory_id,
                    memory_type=candidate.memory_type,
                    content=candidate.content,
                    normalized_content=normalized,
                    project_id=candidate.project_id,
                    provenance=candidate.provenance,
                    evidence=candidate.evidence,
                    status=MemoryStatus.CANDIDATE,  # Will be promoted later
                    created_at=matched_existing.created_at,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    last_validated_at=matched_existing.last_validated_at,
                    expires_at=matched_existing.expires_at,
                    superseded_by=matched_existing.superseded_by,
                    supersedes=matched_existing.supersedes,
                    tags=matched_existing.tags,
                    schema_version=matched_existing.schema_version,
                )

            # Run contradiction detection against existing active memories
            contradiction_findings = detector.detect_contradictions(candidate, existing_records=existing_records)
            blocking_contradictions = [
                f for f in contradiction_findings
                if f.severity == ContradictionSeverity.BLOCKING and f.status == ContradictionStatus.UNRESOLVED
            ]

            if blocking_contradictions:
                # Log contradiction but don't block compilation - let promotion gate handle it
                import logging
                for finding in blocking_contradictions:
                    logging.getLogger(__name__).warning(
                        f"Contradiction detected during compilation for {candidate.memory_id}: "
                        f"{finding.category.value} with {finding.memory_id_b if finding.memory_id_a == candidate.memory_id else finding.memory_id_a}"
                    )

            filtered.append(candidate)

        return filtered

    def _update_memory_provenance(self, existing: MemoryRecord, candidate: MemoryRecord) -> None:
        """Update an existing memory's provenance with new evidence from a candidate.

        This preserves the existing memory ID and merges provenance information
        rather than creating a duplicate record.
        """
        # Merge source experience IDs
        combined_experiences = tuple(
            dict.fromkeys(
                list(existing.provenance.source_experience_ids) +
                list(candidate.provenance.source_experience_ids)
            )
        )

        # Update confidence to max of both
        new_confidence = max(existing.provenance.confidence, candidate.provenance.confidence)

        # Merge evidence (deduplicate by experience_id + excerpt)
        seen_evidence_keys = set()
        combined_evidence = []
        for ev in list(existing.evidence) + list(candidate.evidence):
            key = (ev.experience_id, ev.excerpt)
            if key not in seen_evidence_keys:
                seen_evidence_keys.add(key)
                combined_evidence.append(ev)

        # Create updated provenance
        updated_provenance = MemoryProvenance(
            source_experience_ids=combined_experiences,
            extraction_method=existing.provenance.extraction_method,
            extracted_at=existing.provenance.extracted_at,  # Keep original extraction time
            extractor_agent=existing.provenance.extractor_agent,
            confidence=new_confidence,
        )

        # Create updated memory record
        updated = MemoryRecord(
            memory_id=existing.memory_id,
            memory_type=existing.memory_type,
            content=existing.content,
            normalized_content=normalize_content(existing.normalized_content),
            project_id=existing.project_id,
            provenance=updated_provenance,
            evidence=tuple(combined_evidence),
            status=existing.status,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_validated_at=datetime.now(timezone.utc).isoformat(),
            expires_at=existing.expires_at,
            superseded_by=existing.superseded_by,
            supersedes=existing.supersedes,
            tags=existing.tags,
            schema_version=existing.schema_version,
        )

        # Save the updated record
        self.memory_store.save_record(updated)

    def _content_overlap(self, text1: str, text2: str) -> float:
        """Simple Jaccard similarity for duplicate detection (legacy - kept for compatibility)."""
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        if not words1 or not words2:
            return 0.0
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        return len(intersection) / len(union)

    @classmethod
    def _is_expired_record(cls, record: MemoryRecord) -> bool:
        """Check if a memory record has expired based on expires_at timestamp."""
        if record.expires_at is None:
            return False
        try:
            expires = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return now >= expires
        except Exception:
            # On parse error, treat as not expired (fail open for validity)
            return False

    def promote_candidate(self, memory_id: str, *, reason: str = "Validated by compiler", actor: str = "compiler") -> MemoryRecord | None:
        """Promote a candidate memory to ACTIVE status using centralized promotion policy."""
        record = self.memory_store.load_record(memory_id)
        if not record or record.status != MemoryStatus.CANDIDATE:
            return None

        # Use centralized promotion policy
        policy = MemoryPromotionPolicy(self.project_path)
        promoted, result = policy.enforce_promotion(
            record,
            actor=actor,
            is_human=False,
            dry_run=False,
        )

        if promoted is None:
            # Log rejection reason for debugging
            import logging
            logging.getLogger(__name__).debug(f"Promotion rejected for {memory_id}: {result.reason_code} - {result.message}")
            return None

        return promoted

    def supersede_memory(self, old_memory_id: str, new_memory: MemoryRecord) -> MemoryRecord | None:
        """Supersede an existing memory with a new version.

        Enforces supersession integrity:
        - Both memories must exist
        - Cannot supersede self
        - No cycles in supersession chain
        - Cross-project supersession requires explicit policy (currently rejected)
        - Reciprocal links maintained
        - Cannot supersede EXPIRED or DELETED memories
        """
        old = self.memory_store.load_record(old_memory_id)
        if not old:
            return None

        # Validate: cannot supersede self
        if old_memory_id == new_memory.memory_id:
            raise ValueError("Memory cannot supersede itself")

        # Validate: old memory must be active (not expired/deleted/superseded)
        # Check both status AND expiration timestamp
        if old.status in {MemoryStatus.EXPIRED, MemoryStatus.DELETED, MemoryStatus.SUPERSEDED}:
            raise ValueError(f"Cannot supersede memory in status: {old.status.value}")
        # Also check if ACTIVE memory has expired based on timestamp
        if old.status == MemoryStatus.ACTIVE and self._is_expired_record(old):
            raise ValueError(f"Cannot supersede expired memory: {old_memory_id}")

        # Validate: no cycles in supersession chain
        if self._would_create_cycle(old_memory_id, new_memory.memory_id):
            raise ValueError(f"Supersession would create a cycle: {old_memory_id} -> {new_memory.memory_id}")

        # Validate: cross-project supersession not allowed by default
        if old.project_id != new_memory.project_id:
            raise ValueError(
                f"Cross-project supersession not allowed: "
                f"{old.project_id} -> {new_memory.project_id}"
            )

        # Validate: new memory must not be deleted/expired
        # Check both status AND expiration timestamp
        if new_memory.status in {MemoryStatus.DELETED, MemoryStatus.EXPIRED}:
            raise ValueError(f"Cannot supersede with memory in status: {new_memory.status.value}")
        if new_memory.status == MemoryStatus.ACTIVE and self._is_expired_record(new_memory):
            raise ValueError(f"Cannot supersede with expired memory: {new_memory.memory_id}")

        # Mark old as superseded
        superseded = MemoryRecord(
            memory_id=old.memory_id,
            memory_type=old.memory_type,
            content=old.content,
            normalized_content=old.normalized_content,
            project_id=old.project_id,
            provenance=old.provenance,
            evidence=old.evidence,
            status=MemoryStatus.SUPERSEDED,
            created_at=old.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_validated_at=old.last_validated_at,
            expires_at=old.expires_at,
            superseded_by=new_memory.memory_id,
            supersedes=old.supersedes,
            tags=old.tags,
            schema_version=old.schema_version,
        )
        self.memory_store.save_record(superseded)

        # Save new memory with supersedes link
        updated_new = MemoryRecord(
            memory_id=new_memory.memory_id,
            memory_type=new_memory.memory_type,
            content=new_memory.content,
            normalized_content=new_memory.normalized_content,
            project_id=new_memory.project_id,
            provenance=new_memory.provenance,
            evidence=new_memory.evidence,
            status=new_memory.status,
            created_at=new_memory.created_at,
            updated_at=new_memory.updated_at,
            last_validated_at=new_memory.last_validated_at,
            expires_at=new_memory.expires_at,
            superseded_by=new_memory.superseded_by,
            supersedes=(old_memory_id,) + new_memory.supersedes,
            tags=new_memory.tags,
            schema_version=new_memory.schema_version,
        )
        self.memory_store.save_record(updated_new)

        return updated_new

    def _would_create_cycle(self, old_memory_id: str, new_memory_id: str) -> bool:
        """Check if superseding old_memory_id with new_memory_id would create a cycle.

        A cycle occurs if:
        - The new memory already (directly or indirectly) supersedes the old memory
        - The old memory is already superseded by the new memory (directly)
        - There's a chain leading back to either memory
        """
        # Check 1: old_memory is already superseded by new_memory
        old = self.memory_store.load_record(old_memory_id)
        if old and old.superseded_by == new_memory_id:
            return True

        # Check 2: Walk the supersession chain from new_memory_id
        visited = set()
        current_id = new_memory_id

        while current_id:
            if current_id in visited:
                return True  # Cycle detected
            visited.add(current_id)

            if current_id == old_memory_id:
                return True  # Would create cycle

            current = self.memory_store.load_record(current_id)
            if not current:
                break
            current_id = current.superseded_by

        return False

    def delete_memory(self, memory_id: str, *, hard: bool = False) -> bool:
        """Delete or soft-delete a memory record."""
        record = self.memory_store.load_record(memory_id)
        if not record:
            return False

        if hard:
            # Actually remove file and index entries
            file_path = self.memory_store.records_dir / f"{memory_id}.json"
            if file_path.exists():
                file_path.unlink()
            # Rebuild index to remove
            self.memory_store.build_index(clean=True)
        else:
            # Soft delete - mark as DELETED
            deleted = MemoryRecord(
                memory_id=record.memory_id,
                memory_type=record.memory_type,
                content=record.content,
                normalized_content=record.normalized_content,
                project_id=record.project_id,
                provenance=record.provenance,
                evidence=record.evidence,
                status=MemoryStatus.DELETED,
                created_at=record.created_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                last_validated_at=record.last_validated_at,
                expires_at=record.expires_at,
                superseded_by=record.superseded_by,
                supersedes=record.supersedes,
                tags=record.tags,
                schema_version=record.schema_version,
            )
            self.memory_store.save_record(deleted)

        return True


from datetime import datetime, timezone