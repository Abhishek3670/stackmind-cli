from pathlib import Path
import re
import yaml
from validators.harness.authoring_yaml import repair_unquoted_yaml_scalars
from validators.kernel.daemon.supervisor.dependencies import (
    validate_dependency_graph,
    evaluate_dependencies,
    DependencyStatus,
)


def test_amendments_with_unquoted_colon():
    bad_yaml = """id: WO-002
milestone_id: Milestone 2
type: FEATURE
title: Core Layout & Obsidian Theme
status: ACTIVE
priority: P0
assigned_agents:
- gemini
dependencies: []
deliverable:
  type: code
  path: index.html
  description: Main landing page with Obsidian theme and phone image containers
description: >
  Build the core layout.
acceptance_criteria:
- index.html exists
revision: 6
amendments:
  - revision: 6
    reason: Architect repair: Removing dependency on superseded work order WO-001
"""
    repaired = repair_unquoted_yaml_scalars(bad_yaml)
    data = yaml.safe_load(repaired)
    assert data["id"] == "WO-002"
    assert data["amendments"][0]["revision"] == 6
    assert "Architect repair: Removing dependency" in data["amendments"][0]["reason"]


def test_completed_work_order_not_treated_as_superseded(tmp_path: Path):
    comp_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    comp_dir.mkdir(parents=True, exist_ok=True)
    act_dir.mkdir(parents=True, exist_ok=True)

    # Deliverable for WO-001
    pkg = tmp_path / "package.json"
    pkg.write_text('{"name": "test"}', encoding="utf-8")
    import hashlib
    h = hashlib.sha256(pkg.read_bytes()).hexdigest()

    # WO-001 is completed
    wo1 = {
        "id": "WO-001",
        "revision": 1,
        "status": "COMPLETED",
        "deliverable": {"path": "package.json"},
        "completion": {
            "verified_at_revision": 1,
            "verified_at_contract_revision": 1,
            "deliverable_hashes": {"package.json": h},
            "completed_at": "2026-11-09T12:00:00Z",
        },
        "dependencies": [],
    }
    (comp_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")

    # WO-002 is active and depends on WO-001
    wo2 = {
        "id": "WO-002",
        "status": "ACTIVE",
        "deliverable": {"path": "index.html"},
        "dependencies": ["WO-001"],
    }
    (act_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

    # Even if superseded_ids accidentally includes WO-001, validate_dependency_graph
    # recognizes that WO-001 is COMPLETED on disk with no superseded_by marker.
    issues = validate_dependency_graph(tmp_path, superseded_ids={"WO-001"})
    assert not any(i.code == "SUPERSEDED_DEPENDENCY" for i in issues)

    res = evaluate_dependencies(
        tmp_path,
        "WO-002",
        completed_ids={"WO-001"},
        superseded_ids={"WO-001"},
    )
    assert res.status is DependencyStatus.SATISFIED


def test_wo_deliverable_paths_filters_prose_descriptions():
    from validators.harness.review_gate import wo_deliverable_paths, _is_path_like

    assert not _is_path_like("QA report detailing any layout shifts (CLS) or performance bottlenecks.")
    assert not _is_path_like("Verified cross-browser compatibility report.")
    assert not _is_path_like("Bug fix list for frontend polish.")
    assert _is_path_like("tests/test_qa_verification.py")
    assert _is_path_like("package.json")
    assert _is_path_like("index.html")

    wo = {
        "deliverable": {"type": "code", "path": "tests/test_qa_verification.py"},
        "deliverables": [
            "QA report detailing any layout shifts (CLS) or performance bottlenecks.",
            "Verified cross-browser compatibility report.",
            "Bug fix list for frontend polish.",
        ],
    }
    paths = wo_deliverable_paths(wo)
    assert paths == ["tests/test_qa_verification.py"]

