"""Tests for AuthoringGate deliverable validation and amendment deliverable preservation."""

import pytest
import yaml
from pathlib import Path

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_yaml import normalize_work_order_data
from validators.harness.runner.reconciliation import _ReconciliationMixin


def test_authoring_gate_rejects_feature_work_order_missing_deliverable(tmp_path: Path):
    gate = AuthoringGate(project_root=tmp_path)
    wo_yaml = """
id: WO-001
type: FEATURE
title: Core engine
status: ACTIVE
priority: P0
assigned_agents:
  - codex
dependencies: []
description: Core arithmetic engine
acceptance_criteria:
  - add, subtract, multiply, divide
"""
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        wo_yaml,
        agent="architecture",
        project_root=tmp_path,
    )
    assert not decision.passed
    assert any("requires a deliverable field" in err for err in decision.errors)


def test_authoring_gate_accepts_feature_work_order_with_deliverable(tmp_path: Path):
    gate = AuthoringGate(project_root=tmp_path)
    wo_yaml = """
id: WO-001
type: FEATURE
title: Core engine
status: ACTIVE
priority: P0
assigned_agents:
  - codex
dependencies: []
description: Core arithmetic engine
acceptance_criteria:
  - add, subtract, multiply, divide
deliverable:
  type: code
  path: src/calculator.py
  description: Core calculator module
"""
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        wo_yaml,
        agent="architecture",
        project_root=tmp_path,
    )
    assert decision.passed, f"Errors: {decision.errors}"


def test_normalize_work_order_preserves_prior_deliverable_on_amendment(tmp_path: Path):
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    orig_file = active_dir / "WO-001.yaml"
    orig_file.write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Core engine",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {
                "type": "code",
                "path": "src/calculator.py",
                "description": "Original deliverable",
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    amendment_data = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Core engine",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "revision": 2,
        "amendments": [
            {"revision": 2, "reason": "Bake in contract"}
        ],
        # Notice deliverable is omitted!
    }

    normalized = normalize_work_order_data(amendment_data, project_root=tmp_path)
    assert "deliverable" in normalized
    assert normalized["deliverable"]["path"] == "src/calculator.py"


def test_sync_index_updates_deliverable_for_existing_orders(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo_file = active_dir / "WO-001.yaml"
    wo_file.write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Core engine",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {
                "type": "code",
                "path": "src/calculator.py",
                "description": "Core engine",
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_file.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "orders": [
                {
                    "id": "WO-001",
                    "type": "FEATURE",
                    "title": "Core engine",
                    "status": "ACTIVE",
                    "priority": "P0",
                    "assigned_agents": ["codex"],
                    "dependencies": [],
                    "file": "work-orders/ACTIVE/WO-001.yaml",
                    # No deliverable in index initially
                }
            ],
            "next_id": 2,
            "total_active": 1,
        }, sort_keys=False),
        encoding="utf-8",
    )

    _ReconciliationMixin._sync_index_and_tree_yaml(tmp_path)

    updated_index = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    order_0 = updated_index["orders"][0]
    assert "deliverable" in order_0
    assert order_0["deliverable"]["path"] == "src/calculator.py"
