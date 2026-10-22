"""Unit tests for durable I/O, crash corruption prevention, and auto-fallback recovery."""

import os
import shutil
import time
from pathlib import Path

import pytest
import yaml

from validators.kernel.io import (
    durable_write_text,
    durable_write_yaml,
    is_corrupted_or_zero_filled,
    clean_orphaned_tmp_files,
)
from cli.validate import validate, auto_fix, _load_yaml, _reconstruct_work_order, _repair_completion_notice


def test_durable_write_text_and_yaml(tmp_path: Path):
    target = tmp_path / "subdir" / "test.txt"
    durable_write_text(target, "hello durable world\n")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "hello durable world\n"
    
    # Verify no temp files left behind
    assert list(tmp_path.rglob(".test.txt.tmp.*")) == []
    
    # Test YAML
    yaml_target = tmp_path / "subdir" / "data.yaml"
    data = {"id": "WO-001", "status": "ACTIVE", "items": [1, 2, 3]}
    durable_write_yaml(yaml_target, data)
    assert yaml_target.is_file()
    loaded = yaml.safe_load(yaml_target.read_text(encoding="utf-8"))
    assert loaded == data
    assert list(tmp_path.rglob(".data.yaml.tmp.*")) == []


def test_is_corrupted_or_zero_filled(tmp_path: Path):
    normal_file = tmp_path / "normal.txt"
    normal_file.write_text("valid content", encoding="utf-8")
    assert not is_corrupted_or_zero_filled(normal_file)

    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")
    assert is_corrupted_or_zero_filled(empty_file)

    null_file = tmp_path / "nulls.yaml"
    null_file.write_bytes(b"\x00" * 256)
    assert is_corrupted_or_zero_filled(null_file)


def test_clean_orphaned_tmp_files(tmp_path: Path):
    # Active file
    active = tmp_path / "active.yaml"
    active.write_text("hello", encoding="utf-8")

    # Temp file with old timestamp
    old_tmp = tmp_path / ".active.yaml.tmp.999999.12345"
    old_tmp.write_text("temp", encoding="utf-8")
    # Set mtime back by 100 seconds
    past_time = time.time() - 100.0
    os.utime(old_tmp, (past_time, past_time))

    cleaned = clean_orphaned_tmp_files(tmp_path, max_age_seconds=10.0)
    assert old_tmp in cleaned
    assert not old_tmp.exists()
    assert active.exists()


def test_load_yaml_null_byte_handling(tmp_path: Path):
    null_file = tmp_path / "corrupt.yaml"
    null_file.write_bytes(b"\x00" * 128)

    data, err = _load_yaml(null_file)
    assert data is None
    assert err is not None
    assert "CORRUPTED_NULL_BYTES" in err


def test_work_order_reconstruction_from_index(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    wo_dir = sync_dir / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    index_file = sync_dir / "work-orders" / "INDEX.yaml"

    index_data = {
        "schema_version": 1,
        "orders": [
            {
                "id": "WO-042",
                "type": "FEATURE",
                "title": "Build landing page",
                "status": "ACTIVE",
                "priority": "P0",
                "assigned_agents": ["gemini"],
                "dependencies": [],
                "deliverable": {"type": "code", "path": "index.html"},
                "description": "Build landing page with vivid colors",
                "created": "2026-10-18T00:00:00Z",
                "updated": "2026-10-18T00:00:00Z",
            }
        ]
    }
    index_file.write_text(yaml.safe_dump(index_data), encoding="utf-8")

    corrupt_wo = wo_dir / "WO-042.yaml"
    corrupt_wo.write_bytes(b"\x00" * 300)

    # Reconstruct
    success = _reconstruct_work_order(sync_dir, "WO-042", corrupt_wo)
    assert success is True
    assert corrupt_wo.is_file()

    rebuilt = yaml.safe_load(corrupt_wo.read_text(encoding="utf-8"))
    assert rebuilt["id"] == "WO-042"
    assert rebuilt["title"] == "Build landing page"
    assert rebuilt["status"] == "ACTIVE"
    assert rebuilt["deliverable"]["path"] == "index.html"


def test_repair_completion_notice(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    inbox_dir = sync_dir / "inbox" / "claude"
    inbox_dir.mkdir(parents=True)

    # Setup version
    (tmp_path / "VERSION").write_text("v2.5.0\n", encoding="utf-8")

    # Corrupt completion notice
    notice_file = inbox_dir / "2026-10-18_claude_WO-000-complete.md"
    notice_file.write_bytes(b"\x00" * 150)

    success = _repair_completion_notice(sync_dir, notice_file)
    assert success is True
    assert notice_file.is_file()

    text = notice_file.read_text(encoding="utf-8")
    assert "release_target: v2.5.0" in text
    assert "WO ID: WO-000" in text
    assert "Status: completed" in text


def test_validate_and_auto_fix_corrupted_workspace(tmp_path: Path):
    """Simulate a project that crashed and left null-byte files; verify validate --fix restores it."""
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    
    # Minimal protocol scaffolding
    (sync_dir / "PROTOCOL_DIGEST.md").write_text("# Protocol", encoding="utf-8")
    (sync_dir / "PROTOCOL_DIGEST.hash").write_text("abc", encoding="utf-8")
    (sync_dir / "RUNTIME_VERSION").write_text('{"version": "3.1.0"}', encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    
    (sync_dir / "runtime").mkdir()
    tree_data = {"schema_version": 1, "tree_version": 1, "release": "1.0.0", "work_orders": {"total_active": 1, "total_completed": 0, "total_blocked": 0}}
    (sync_dir / "runtime" / "TREE.yaml").write_text(yaml.safe_dump(tree_data), encoding="utf-8")

    wo_dir = sync_dir / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_data = {
        "schema_version": 1,
        "orders": [
            {
                "id": "WO-000",
                "type": "RESEARCH",
                "title": "Architecture planning",
                "status": "ACTIVE",
                "priority": "P0",
                "assigned_agents": ["claude"],
                "dependencies": [],
                "created": "2026-10-18T00:00:00Z",
                "updated": "2026-10-18T00:00:00Z",
            }
        ],
        "total_active": 1,
        "total_completed": 0,
        "total_blocked": 0,
    }
    index_file.write_text(yaml.safe_dump(index_data), encoding="utf-8")

    # Corrupt WO-000.yaml
    corrupt_wo = wo_dir / "WO-000.yaml"
    corrupt_wo.write_bytes(b"\x00" * 500)

    # Corrupt completion notice
    inbox_dir = sync_dir / "inbox" / "claude"
    inbox_dir.mkdir(parents=True)
    corrupt_notice = inbox_dir / "2026-10-18_claude_WO-000-complete.md"
    corrupt_notice.write_bytes(b"\x00" * 200)

    # 1. Validation detects the corrupted files as auto-fixable
    result = validate(tmp_path, fix=False)
    fixable = [i for i in result.issues if i.auto_fixable]
    assert len(fixable) >= 2

    # 2. Run auto_fix
    fixed_count = auto_fix(sync_dir, fixable)
    assert fixed_count >= 2

    # 3. Verify files are now valid
    assert b"\x00" not in corrupt_wo.read_bytes()
    rebuilt_wo = yaml.safe_load(corrupt_wo.read_text(encoding="utf-8"))
    assert rebuilt_wo["id"] == "WO-000"

    assert b"\x00" not in corrupt_notice.read_bytes()
    assert "release_target: v1.0.0" in corrupt_notice.read_text(encoding="utf-8")


def test_tui_startup_auto_recovers_corrupted_project(tmp_path: Path, monkeypatch):
    """Verify cli.tui_startup.prepare_project seamlessly auto-recovers corrupted files."""
    from cli.init import init
    import cli.tui_startup as startup

    monkeypatch.setattr(startup, "graph_needs_build", lambda _workspace: False)
    monkeypatch.setattr(startup, "build_knowledge_graph", lambda _workspace: None)

    # Initialize a valid StackMind runtime
    init(tmp_path, no_git=True)
    sync_dir = tmp_path / ".sync"

    # Add WO-000 to INDEX.yaml and TREE.yaml
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    index_data["orders"].append({
        "id": "WO-000",
        "type": "RESEARCH",
        "title": "Architecture planning",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["claude"],
        "dependencies": [],
        "created": "2026-10-18T00:00:00Z",
        "updated": "2026-10-18T00:00:00Z",
    })
    index_data["total_active"] = 1
    index_file.write_text(yaml.safe_dump(index_data), encoding="utf-8")

    tree_file = sync_dir / "runtime" / "TREE.yaml"
    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    tree_data["work_orders"]["total_active"] = 1
    tree_file.write_text(yaml.safe_dump(tree_data), encoding="utf-8")

    # Corrupted null-byte work order
    wo_dir = sync_dir / "work-orders" / "ACTIVE"
    corrupt_wo = wo_dir / "WO-000.yaml"
    corrupt_wo.write_bytes(b"\x00" * 500)

    # Corrupted null-byte notice
    inbox_dir = sync_dir / "inbox" / "claude"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    corrupt_notice = inbox_dir / "2026-10-18_claude_WO-000-complete.md"
    corrupt_notice.write_bytes(b"\x00" * 200)

    # Orphaned dead pid and temp file
    daemon_dir = sync_dir / "runtime" / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "daemon.pid").write_text("999999", encoding="utf-8")
    (daemon_dir / "daemon-state.tmp.999999.123").write_text("{}", encoding="utf-8")

    # prepare_project must NOT raise ClickException; it should heal and pass
    startup.prepare_project(tmp_path, yes=True, interactive=False)

    # Verify corrupt_wo is healed
    assert b"\x00" not in corrupt_wo.read_bytes()
    data = yaml.safe_load(corrupt_wo.read_text(encoding="utf-8"))
    assert data["id"] == "WO-000"

    # Verify corrupt_notice is healed
    assert b"\x00" not in corrupt_notice.read_bytes()
    assert "release_target:" in corrupt_notice.read_text(encoding="utf-8")

    # Verify dead daemon temp files cleaned
    assert not (daemon_dir / "daemon.pid").exists()
    assert not (daemon_dir / "daemon-state.tmp.999999.123").exists()

