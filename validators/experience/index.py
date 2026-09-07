"""Rebuildable Tier 2 SQLite & FTS5 compilation index for Experience Records.

Implements Phase 2 (Experience Compilation) of Verified Procedural Learning.
Enforces the "rebuildable derived cache" invariant: the database is derived entirely
from raw Tier 1 records under .sync/experience/records/ and can be cleanly rebuilt at any time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from validators.experience.models import ExperienceRecord
from validators.experience.store import ExperienceStore
from validators.harness.snapshot import TrustLevel


@dataclass(frozen=True)
class ExperienceSearchResult:
    """A ranked search hit from the experience full-text compilation index."""

    experience_id: str
    task_id: str
    agent_id: str
    task_signature: str
    trust_level: TrustLevel
    learning_eligible: bool
    outcome: str
    recorded_at: str
    rank: float
    matched_snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "experience_id": self.experience_id,
            "learning_eligible": self.learning_eligible,
            "matched_snippet": self.matched_snippet,
            "outcome": self.outcome,
            "rank": self.rank,
            "recorded_at": self.recorded_at,
            "task_id": self.task_id,
            "task_signature": self.task_signature,
            "trust_level": self.trust_level.value if hasattr(self.trust_level, "value") else str(self.trust_level),
        }


class ExperienceIndex:
    """Derived SQLite + FTS5 index over raw experience records."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.cache_dir = self.project_path / ".sync" / "experience" / "cache"
        self.db_path = self.cache_dir / "experience_index.db"
        self.store = ExperienceStore(self.project_path)

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
            CREATE TABLE IF NOT EXISTS experience_entries (
                experience_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                work_order_id TEXT,
                task_signature TEXT NOT NULL,
                trust_level TEXT NOT NULL,
                learning_eligible INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                files_modified_count INTEGER NOT NULL,
                actions_count INTEGER NOT NULL,
                failures_count INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_exp_agent ON experience_entries(agent_id);
            CREATE INDEX IF NOT EXISTS idx_exp_eligible ON experience_entries(learning_eligible);
            CREATE INDEX IF NOT EXISTS idx_exp_trust ON experience_entries(trust_level);
            CREATE INDEX IF NOT EXISTS idx_exp_sig ON experience_entries(task_signature);
            CREATE INDEX IF NOT EXISTS idx_exp_wo ON experience_entries(work_order_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS experience_fts USING fts5(
                experience_id UNINDEXED,
                task_signature,
                task_id,
                actions_text,
                observations_text,
                failures_text,
                summary_text,
                modified_files_text
            );
            """
        )

        if should_close:
            conn.close()

    def build_index(self, *, clean: bool = False) -> int:
        """Rebuild or initialize the index completely from Tier 1 raw records."""
        if clean and self.db_path.exists():
            try:
                self.db_path.unlink()
            except OSError:
                pass

        conn = self._get_connection()
        self.init_schema(conn)

        records = self.store.list_records()
        with conn:
            conn.execute("DELETE FROM experience_fts;")
            conn.execute("DELETE FROM experience_entries;")

            for record in records:
                self._insert_record(conn, record)

        indexed_count = len(records)
        conn.close()
        return indexed_count

    def update_index(self) -> tuple[int, int]:
        """Incrementally sync the SQLite index with the filesystem records."""
        conn = self._get_connection()
        self.init_schema(conn)

        # Get existing indexed records and hashes
        cursor = conn.execute("SELECT experience_id, record_hash FROM experience_entries;")
        indexed_hashes = {row["experience_id"]: row["record_hash"] for row in cursor.fetchall()}

        records = self.store.list_records()
        current_ids = set()
        upsert_count = 0

        with conn:
            for record in records:
                current_ids.add(record.experience_id)
                raw_bytes = json.dumps(record.to_dict(), sort_keys=True).encode("utf-8")
                current_hash = hashlib.sha256(raw_bytes).hexdigest()

                if indexed_hashes.get(record.experience_id) != current_hash:
                    # Update or Insert
                    conn.execute("DELETE FROM experience_fts WHERE experience_id = ?;", (record.experience_id,))
                    conn.execute("DELETE FROM experience_entries WHERE experience_id = ?;", (record.experience_id,))
                    self._insert_record(conn, record, record_hash=current_hash)
                    upsert_count += 1

            # Remove deleted records
            deleted_ids = set(indexed_hashes.keys()) - current_ids
            for del_id in deleted_ids:
                conn.execute("DELETE FROM experience_fts WHERE experience_id = ?;", (del_id,))
                conn.execute("DELETE FROM experience_entries WHERE experience_id = ?;", (del_id,))

        deleted_count = len(deleted_ids)
        conn.close()
        return upsert_count, deleted_count

    def _insert_record(
        self,
        conn: sqlite3.Connection,
        record: ExperienceRecord,
        *,
        record_hash: str | None = None,
    ) -> None:
        raw_dict = record.to_dict()
        if record_hash is None:
            raw_bytes = json.dumps(raw_dict, sort_keys=True).encode("utf-8")
            record_hash = hashlib.sha256(raw_bytes).hexdigest()

        actions_text = " ".join(f"{a.tool} {a.command_or_symbol} {a.input_summary}" for a in record.actions)
        observations_text = " ".join(f"{o.output_summary} {o.error_type or ''}" for o in record.observations)
        failures_text = " ".join(f"{f.phase} {f.error_message}" for f in record.failures)
        summary_text = record.final_state.get("summary", "")
        modified_files_text = " ".join(record.verification.observed_diff.all_changed_files)

        cursor = conn.execute(
            """
            INSERT INTO experience_entries (
                experience_id, task_id, agent_id, work_order_id,
                task_signature, trust_level, learning_eligible, outcome,
                duration_ms, recorded_at, files_modified_count, actions_count,
                failures_count, record_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record.experience_id,
                record.task_id,
                record.agent_id,
                record.work_order_id,
                record.task_signature,
                record.trust_level.value,
                1 if record.learning_eligible else 0,
                record.outcome,
                record.duration_ms,
                record.recorded_at,
                len(record.verification.observed_diff.all_changed_files),
                len(record.actions),
                len(record.failures),
                record_hash,
                json.dumps(raw_dict, sort_keys=True),
            ),
        )

        rowid = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO experience_fts (
                rowid, experience_id, task_signature, task_id,
                actions_text, observations_text, failures_text,
                summary_text, modified_files_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                rowid,
                record.experience_id,
                record.task_signature,
                record.task_id,
                actions_text,
                observations_text,
                failures_text,
                summary_text,
                modified_files_text,
            ),
        )

    def search(
        self,
        query: str,
        *,
        learning_eligible_only: bool = False,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> list[ExperienceSearchResult]:
        """Perform full-text BM25 search over compiled experience records."""
        if not self.db_path.exists():
            self.build_index()

        conn = self._get_connection()
        sanitized_query = query.replace('"', '""').strip()
        if not sanitized_query:
            return []

        # Safe FTS token matching
        tokens = [f'"{t}"' for t in sanitized_query.split() if t.isalnum() or "-" in t or "_" in t]
        if not tokens:
            tokens = [f'"{sanitized_query}"']
        match_expr = " OR ".join(tokens)

        filters = []
        params: list[Any] = [match_expr]

        if learning_eligible_only:
            filters.append("e.learning_eligible = 1")
        if agent_id:
            filters.append("e.agent_id = ?")
            params.append(agent_id)

        where_clause = f"AND {' AND '.join(filters)}" if filters else ""
        params.append(limit)

        sql = f"""
            SELECT
                e.experience_id,
                e.task_id,
                e.agent_id,
                e.task_signature,
                e.trust_level,
                e.learning_eligible,
                e.outcome,
                e.recorded_at,
                bm25(experience_fts) AS rank,
                snippet(experience_fts, 3, '[match]', '[/match]', '...', 16) AS matched_snippet
            FROM experience_fts f
            JOIN experience_entries e ON f.experience_id = e.experience_id
            WHERE experience_fts MATCH ?
            {where_clause}
            ORDER BY rank ASC, e.recorded_at DESC
            LIMIT ?;
        """

        results: list[ExperienceSearchResult] = []
        try:
            cursor = conn.execute(sql, params)
            for row in cursor.fetchall():
                results.append(
                    ExperienceSearchResult(
                        experience_id=row["experience_id"],
                        task_id=row["task_id"],
                        agent_id=row["agent_id"],
                        task_signature=row["task_signature"],
                        trust_level=TrustLevel(row["trust_level"]),
                        learning_eligible=bool(row["learning_eligible"]),
                        outcome=row["outcome"],
                        recorded_at=row["recorded_at"],
                        rank=float(row["rank"]),
                        matched_snippet=row["matched_snippet"],
                    )
                )
        except sqlite3.OperationalError:
            # Fallback to direct SQL LIKE query if FTS expression is complex
            sql_fallback = f"""
                SELECT
                    e.experience_id, e.task_id, e.agent_id, e.task_signature,
                    e.trust_level, e.learning_eligible, e.outcome, e.recorded_at,
                    0.0 as rank, NULL as matched_snippet
                FROM experience_entries e
                WHERE (e.task_signature LIKE ? OR e.task_id LIKE ? OR e.raw_json LIKE ?)
                {where_clause}
                ORDER BY e.recorded_at DESC
                LIMIT ?;
            """
            like_param = f"%{sanitized_query}%"
            fallback_params = [like_param, like_param, like_param] + params[1:]
            cursor = conn.execute(sql_fallback, fallback_params)
            for row in cursor.fetchall():
                results.append(
                    ExperienceSearchResult(
                        experience_id=row["experience_id"],
                        task_id=row["task_id"],
                        agent_id=row["agent_id"],
                        task_signature=row["task_signature"],
                        trust_level=TrustLevel(row["trust_level"]),
                        learning_eligible=bool(row["learning_eligible"]),
                        outcome=row["outcome"],
                        recorded_at=row["recorded_at"],
                        rank=0.0,
                        matched_snippet=None,
                    )
                )
        finally:
            conn.close()

        return results

    def query_by_file(self, file_path: str, *, limit: int = 10) -> list[ExperienceRecord]:
        """Find experiences that touched a specific file path."""
        conn = self._get_connection()
        self.init_schema(conn)
        norm_path = file_path.replace("\\", "/").strip("/")
        cursor = conn.execute(
            """
            SELECT raw_json FROM experience_entries
            WHERE raw_json LIKE ?
            ORDER BY recorded_at DESC
            LIMIT ?;
            """,
            (f'%"{norm_path}"%', limit),
        )
        records = []
        for row in cursor.fetchall():
            try:
                data = json.loads(row["raw_json"])
                records.append(ExperienceRecord.from_dict(data))
            except Exception:
                continue
        conn.close()
        return records

    def stats(self) -> dict[str, Any]:
        """Return compilation index statistics."""
        if not self.db_path.exists():
            return {"indexed": 0, "db_size_bytes": 0, "status": "unbuilt"}

        conn = self._get_connection()
        self.init_schema(conn)
        cursor = conn.execute(
            """
            SELECT
                count(*) as total,
                sum(learning_eligible) as eligible,
                sum(case when trust_level = 'VERIFIED' then 1 else 0 end) as verified,
                sum(case when trust_level = 'OBSERVABLE' then 1 else 0 end) as observable
            FROM experience_entries;
            """
        )
        row = cursor.fetchone()
        stats_data = {
            "db_size_bytes": self.db_path.stat().st_size,
            "eligible": int(row["eligible"] or 0),
            "indexed": int(row["total"] or 0),
            "observable": int(row["observable"] or 0),
            "status": "ready",
            "verified": int(row["verified"] or 0),
        }
        conn.close()
        return stats_data
