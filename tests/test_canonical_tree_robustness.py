"""Tests for canonical TREE.yaml robustness, auto-healing, and schema protection."""
import json
from pathlib import Path
import pytest
import yaml

from cli.init import ensure_canonical_tree, ensure_workspace_protocol_scaffolding
from cli.validate import validate, validate_schema, ValidationResult


def test_ensure_canonical_tree_reconstructs_from_missing(tmp_path: Path):
    """When .sync/runtime/TREE.yaml does not exist, ensure_canonical_tree creates a valid canonical tree."""
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    tree_file = sync_dir / "runtime" / "TREE.yaml"

    assert not tree_file.exists()
    tree_data = ensure_canonical_tree(tmp_path)

    assert tree_file.is_file()
    assert tree_data["schema_version"] == 1
    assert tree_data["tree_version"] >= 1
    assert tree_data["release"] == "3.1.0"
    assert "agents" in tree_data
    assert "claude" in tree_data["agents"]
    assert "gemma" in tree_data["agents"]
    assert "work_orders" in tree_data


def test_ensure_canonical_tree_heals_truncated_fragment(tmp_path: Path):
    """Regression for clean_tui_test: when TREE.yaml is truncated to just agents & last_updated,
    ensure_canonical_tree preserves existing agent entries and restores required root schema fields."""
    sync_dir = tmp_path / ".sync"
    runtime_dir = sync_dir / "runtime"
    runtime_dir.mkdir(parents=True)
    tree_file = runtime_dir / "TREE.yaml"

    # Exact 7-line fragment that corrupted clean_tui_test
    fragment = {
        "last_updated": "2026-10-21T12:46:55.089581+00:00",
        "agents": {
            "gemma": {
                "session_count": 0,
                "status": "idle",
                "assigned_work_orders": ["WO-004"],
            }
        },
    }
    tree_file.write_text(yaml.safe_dump(fragment), encoding="utf-8")

    tree_data = ensure_canonical_tree(tmp_path)

    # Required fields restored
    assert tree_data["schema_version"] == 1
    assert tree_data["tree_version"] == 1
    assert tree_data["release"] == "3.1.0"
    assert "work_orders" in tree_data

    # Gemma's existing work order preserved
    assert "gemma" in tree_data["agents"]
    assert tree_data["agents"]["gemma"]["assigned_work_orders"] == ["WO-004"]


def test_scaffolding_ensures_valid_tree(tmp_path: Path):
    """ensure_workspace_protocol_scaffolding guarantees valid TREE.yaml on disk."""
    ws = tmp_path / "project"
    ws.mkdir()
    ensure_workspace_protocol_scaffolding(ws)

    tree_file = ws / ".sync" / "runtime" / "TREE.yaml"
    assert tree_file.is_file()

    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assert tree_data["schema_version"] == 1
    assert tree_data["tree_version"] == 1
    assert "agents" in tree_data


def test_archive_completed_work_orders_preserves_canonical_tree(tmp_path: Path):
    """archive_completed_work_orders never strips schema_version, tree_version, or release."""
    from validators.kernel.daemon.supervisor import archive_completed_work_orders
    ensure_workspace_protocol_scaffolding(tmp_path)

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    wo_file = active_dir / "WO-001.yaml"
    wo_file.write_text(yaml.safe_dump({"id": "WO-001", "status": "COMPLETED", "title": "Test"}), encoding="utf-8")

    archived = archive_completed_work_orders(tmp_path, ["WO-001"])
    assert "WO-001" in archived

    tree_file = tmp_path / ".sync" / "runtime" / "TREE.yaml"
    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assert tree_data["schema_version"] == 1
    assert tree_data["tree_version"] >= 1
    assert tree_data["release"] == "3.1.0"
    assert "work_orders" in tree_data
    assert "agents" in tree_data
