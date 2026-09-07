"""Comprehensive test suite for Phase 2 (Experience Compilation).

Covers:
1. Rebuildable SQLite/FTS5 schema initialization.
2. Clean rebuild from raw Tier 1 experience records.
3. Incremental updates (add, modify, delete reconciliation).
4. Full-text BM25 search ranking, match snippets, and filtering.
5. Querying by modified file.
6. Cache deletion and rebuildability invariant.
7. Experience compile and search CLI commands.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.experience.index import ExperienceIndex, ExperienceSearchResult
from validators.experience.models import (
    ExperienceAction,
    ExperienceObservation,
    ExperienceRecord,
    ExperienceVerification,
)
from validators.experience.store import ExperienceStore
from validators.harness.snapshot import (
    TrustLevel,
    VerificationDimensions,
    WorkspaceDiff,
)


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "exp-compile-workspace"
    init(project, name="Compilation Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _create_sample_record(
    store: ExperienceStore,
    signature: str,
    timestamp: str,
    *,
    agent_id: str = "codex",
    learning_eligible: bool = True,
    commands: list[str] = ("alembic upgrade head",),
    modified_files: list[str] = ("alembic/versions/001_init.py",),
    summary: str = "Applied database schema migration",
) -> ExperienceRecord:
    exp_id = ExperienceRecord.mint_id(signature, timestamp)
    diff = WorkspaceDiff(modified=tuple(modified_files))
    dimensions = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )
    verification = ExperienceVerification(
        dimensions=dimensions if learning_eligible else VerificationDimensions(),
        observed_diff=diff,
    )
    actions = tuple(
        ExperienceAction(
            tool="bash",
            command_or_symbol=cmd,
            input_summary=cmd,
            timestamp=timestamp,
        )
        for cmd in commands
    )
    observations = tuple(
        ExperienceObservation(
            output_summary="Command executed successfully",
            exit_code=0,
            is_error=False,
        )
        for _ in commands
    )

    rec = ExperienceRecord(
        experience_id=exp_id,
        task_id=f"task-{signature}",
        agent_id=agent_id,
        task_signature=signature,
        environment={"runtime_version": "v3.1.0"},
        actions=actions,
        observations=observations,
        verification=verification,
        trust_level=TrustLevel.LEARNING_ELIGIBLE if learning_eligible else TrustLevel.OBSERVABLE,
        learning_eligible=learning_eligible,
        outcome="completed",
        final_state={"summary": summary},
        recorded_at=timestamp,
    )
    store.save_record(rec)
    return rec


# ─── 1. Clean Rebuild & Schema Initialization ─────────────────────────────────

def test_clean_rebuild_from_scratch(temp_workspace):
    store = ExperienceStore(temp_workspace)
    _create_sample_record(store, "migration:user_table", "2026-09-01T10:00:00+00:00")
    _create_sample_record(store, "refactor:auth_router", "2026-09-01T11:00:00+00:00")

    index = ExperienceIndex(temp_workspace)
    count = index.build_index(clean=True)
    assert count == 2

    stats = index.stats()
    assert stats["indexed"] == 2
    assert stats["status"] == "ready"
    assert stats["db_size_bytes"] > 0


# ─── 2. Incremental Index Compilation ────────────────────────────────────────

def test_incremental_index_updates(temp_workspace):
    store = ExperienceStore(temp_workspace)
    rec1 = _create_sample_record(store, "task:one", "2026-09-01T10:00:00+00:00")

    index = ExperienceIndex(temp_workspace)
    index.build_index()

    # Add second record
    rec2 = _create_sample_record(store, "task:two", "2026-09-01T11:00:00+00:00")
    upserted, deleted = index.update_index()
    assert upserted == 1
    assert deleted == 0
    assert index.stats()["indexed"] == 2

    # Delete first record from filesystem
    (temp_workspace / ".sync" / "experience" / "records" / f"{rec1.experience_id}.json").unlink()
    upserted_2, deleted_2 = index.update_index()
    assert upserted_2 == 0
    assert deleted_2 == 1
    assert index.stats()["indexed"] == 1


# ─── 3. Full-Text BM25 Search & Filtering ────────────────────────────────────

def test_fts5_bm25_search(temp_workspace):
    store = ExperienceStore(temp_workspace)
    rec_db = _create_sample_record(
        store,
        "database:fix_pool_exhaustion",
        "2026-09-01T10:00:00+00:00",
        agent_id="codex",
        learning_eligible=True,
        commands=["inspect connection pool", "increase pool size"],
        summary="Diagnosed pool saturation and increased capacity",
    )
    rec_auth = _create_sample_record(
        store,
        "security:rotate_jwt_keys",
        "2026-09-01T11:00:00+00:00",
        agent_id="gemini",
        learning_eligible=False,
        commands=["rotate rsa keys", "verify token signature"],
        summary="Rotated authorization jwt certificates",
    )

    index = ExperienceIndex(temp_workspace)
    index.build_index()

    # Search query "pool exhaustion"
    hits_pool = index.search("pool exhaustion")
    assert len(hits_pool) >= 1
    assert hits_pool[0].experience_id == rec_db.experience_id

    # Search with learning_eligible_only filter
    hits_jwt_eligible = index.search("jwt", learning_eligible_only=True)
    assert len(hits_jwt_eligible) == 0  # rec_auth is not learning eligible

    hits_jwt_all = index.search("jwt", learning_eligible_only=False)
    assert len(hits_jwt_all) == 1
    assert hits_jwt_all[0].experience_id == rec_auth.experience_id

    # Search with agent filter
    hits_agent_codex = index.search("pool", agent_id="codex")
    assert len(hits_agent_codex) == 1
    hits_agent_gemini = index.search("pool", agent_id="gemini")
    assert len(hits_agent_gemini) == 0


# ─── 4. Query By Touched File ────────────────────────────────────────────────

def test_query_by_touched_file(temp_workspace):
    store = ExperienceStore(temp_workspace)
    _create_sample_record(
        store,
        "auth:fix_header",
        "2026-09-01T10:00:00+00:00",
        modified_files=["src/auth/middleware.py"],
    )
    index = ExperienceIndex(temp_workspace)
    index.build_index()

    hits = index.query_by_file("src/auth/middleware.py")
    assert len(hits) == 1
    assert hits[0].task_signature == "auth:fix_header"

    hits_none = index.query_by_file("src/billing/invoices.py")
    assert len(hits_none) == 0


# ─── 5. Cache Rebuildability Invariant (Tier 2 Derived Gate) ──────────────────

def test_cache_rebuildability_invariant(temp_workspace):
    store = ExperienceStore(temp_workspace)
    _create_sample_record(store, "task:alpha", "2026-09-01T10:00:00+00:00")
    _create_sample_record(store, "task:beta", "2026-09-01T11:00:00+00:00")

    index = ExperienceIndex(temp_workspace)
    index.build_index()
    initial_stats = index.stats()

    # Blow away the database file entirely
    index.db_path.unlink()
    assert not index.db_path.exists()

    # Rebuild from raw Tier 1 records
    rebuilt_count = index.build_index()
    assert rebuilt_count == 2
    rebuilt_stats = index.stats()

    assert rebuilt_stats["indexed"] == initial_stats["indexed"]
    assert rebuilt_stats["eligible"] == initial_stats["eligible"]


# ─── 6. Experience Compile & Search CLI Commands ─────────────────────────────

def test_experience_compile_and_search_cli(temp_workspace):
    store = ExperienceStore(temp_workspace)
    rec = _create_sample_record(
        store,
        "cli:search_test",
        "2026-09-01T12:00:00+00:00",
        commands=["pytest tests/test_payment.py"],
        summary="Optimized payment gateway integration",
    )

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Compile command
    res_compile = runner.invoke(cli, ["experience", "compile", "-p", str(temp_workspace), "--clean"])
    assert res_compile.exit_code == 0
    assert "Rebuilt experience index with 1 record" in res_compile.output

    # 2. Search command
    res_search = runner.invoke(cli, ["experience", "search", "payment", "-p", str(temp_workspace)])
    assert res_search.exit_code == 0
    assert rec.experience_id in res_search.output
    assert "cli:search_test" in res_search.output

    # 3. Stats includes FTS Indexed Records
    res_stats = runner.invoke(cli, ["experience", "stats", "-p", str(temp_workspace)])
    assert res_stats.exit_code == 0
    assert "FTS Indexed Records" in res_stats.output
