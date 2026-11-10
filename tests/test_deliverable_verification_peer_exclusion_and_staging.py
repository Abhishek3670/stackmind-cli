"""Regression test for Single Deliverable Ownership, peer exclusion, and staged verification."""

from pathlib import Path
from tempfile import TemporaryDirectory
import yaml

from validators.harness.deliverable_verifier import verify_work_order_deliverables
from validators.harness.task_contract import collect_peer_deliverables


def test_collect_peer_deliverables_ignores_cancelled_and_superseded(tmp_path: Path):
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # Active peer
    (active_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "status": "ACTIVE",
            "deliverable": {"path": "assets/js/scroll.js"},
        }),
        encoding="utf-8",
    )

    # Cancelled peer
    (active_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003",
            "status": "CANCELLED",
            "deliverable": {"path": "cancelled.js"},
        }),
        encoding="utf-8",
    )

    # Superseded peer
    (active_dir / "WO-004.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-004",
            "status": "SUPERSEDED",
            "deliverable": {"path": "superseded.js"},
        }),
        encoding="utf-8",
    )

    peers = collect_peer_deliverables(tmp_path, "WO-001")
    peer_paths = [p for _, p in peers]
    assert "assets/js/scroll.js" in peer_paths
    assert "cancelled.js" not in peer_paths
    assert "superseded.js" not in peer_paths


def test_verify_work_order_deliverables_excludes_peer_owned_deliverables(tmp_path: Path):
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # Peer WO-003 owns index.html
    (active_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003",
            "status": "ACTIVE",
            "deliverable": {"path": "index.html"},
        }),
        encoding="utf-8",
    )

    # Pre-existing project files
    (tmp_path / "README.md").write_text("# Velocity\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    # WO-001 mistakenly has index.html in deliverables
    wo_data = {
        "id": "WO-001",
        "status": "ACTIVE",
        "deliverable": {"path": "package.json", "type": "code"},
        "deliverables": [
            {"path": "package.json", "type": "code"},
            {"path": ".gitignore", "type": "code"},
            {"path": "index.html", "type": "code"},  # Owned by peer WO-003!
            {"path": "README.md", "type": "doc"},
        ],
    }

    # Simulate turn in staged scratch directory where only package.json was written
    with TemporaryDirectory() as staged_dir:
        staged = Path(staged_dir)
        (staged / "package.json").write_text('{"name": "test"}', encoding="utf-8")

        passed, errors, evidence = verify_work_order_deliverables(
            tmp_path, wo_data, staged_root=staged
        )
        assert passed is True, f"Failed with errors: {errors}"
        assert errors == []
        # index.html was excluded from declared_paths
        assert "index.html" not in evidence["declared_paths"]
        assert "package.json" in evidence["declared_paths"]
        assert ".gitignore" in evidence["declared_paths"]
        assert "README.md" in evidence["declared_paths"]


def test_staged_verification_falls_back_to_project_path_for_preexisting_files(tmp_path: Path):
    # Pre-existing file in project root
    (tmp_path / "README.md").write_text("# Project Documentation\n", encoding="utf-8")

    wo_data = {
        "id": "WO-001",
        "status": "ACTIVE",
        "deliverable": {"path": "package.json", "type": "code"},
        "deliverables": [
            {"path": "package.json", "type": "code"},
            {"path": "README.md", "type": "doc"},
        ],
    }

    # Staged scratch directory only has package.json
    with TemporaryDirectory() as staged_dir:
        staged = Path(staged_dir)
        (staged / "package.json").write_text('{"name": "test"}', encoding="utf-8")

        passed, errors, evidence = verify_work_order_deliverables(
            tmp_path, wo_data, staged_root=staged
        )
        assert passed is True
        assert errors == []
        assert evidence["passed_count"] == 2
