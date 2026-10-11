"""Integration tests for D024 gate with content verification."""

import json
import pytest
from pathlib import Path


class TestD024WithContentVerification:
    """Tests that D024 gate performs content verification on deliverables."""

    def test_d024_blocks_deliverable_with_placeholder(self, tmp_path):
        """D024 should block release when deliverable has unresolved placeholders."""
        from validators.harness.d024_gate import D024Gate
        import yaml

        # Create work order with deliverable
        sync_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        sync_dir.mkdir(parents=True)

        wo_path = sync_dir / "WO-001.yaml"
        wo_data = {
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Implement feature",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "deliverable": {
                "type": "code",
                "path": "src/auth.py",
            },
        }
        wo_path.write_text(yaml.dump(wo_data))

        # Create deliverable with placeholder
        deliverable = tmp_path / "src" / "auth.py"
        deliverable.parent.mkdir()
        deliverable.write_text("# TODO: Implement authentication\n")

        # Create QA verdict
        qa_dir = tmp_path / ".sync" / "qa" / "verdicts"
        qa_dir.mkdir(parents=True)
        verdict = qa_dir / "WO-001_verdict.md"
        verdict.write_text("# Verdict: APPROVED\n")

        gate = D024Gate()
        decision = gate.evaluate_work_order(tmp_path, "WO-001")

        # Should fail on placeholder content check
        assert not decision.passed
        assert "placeholder" in decision.reason.lower() or "content verification" in decision.reason.lower()

    def test_d024_blocks_malformed_json(self, tmp_path):
        """D024 should block when deliverable has invalid JSON."""
        from validators.harness.d024_gate import D024Gate
        import yaml

        sync_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        sync_dir.mkdir(parents=True)

        wo_path = sync_dir / "WO-002.yaml"
        wo_data = {
            "id": "WO-002",
            "type": "CONFIG",
            "title": "Config update",
            "status": "ACTIVE",
            "deliverable": {
                "type": "config",
                "path": "config.json",
            },
        }
        wo_path.write_text(yaml.dump(wo_data))

        deliverable = tmp_path / "config.json"
        deliverable.write_text("{invalid json}")

        qa_dir = tmp_path / ".sync" / "qa" / "verdicts"
        qa_dir.mkdir(parents=True)
        verdict = qa_dir / "WO-002_verdict.md"
        verdict.write_text("# Verdict: APPROVED\n")

        gate = D024Gate()
        decision = gate.evaluate_work_order(tmp_path, "WO-002")

        assert not decision.passed
        assert "json" in decision.reason.lower()

    def test_d024_passes_valid_deliverable(self, tmp_path):
        """D024 should pass when deliverable is valid."""
        from validators.harness.d024_gate import D024Gate
        import yaml

        sync_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        sync_dir.mkdir(parents=True)

        wo_path = sync_dir / "WO-003.yaml"
        wo_data = {
            "id": "WO-003",
            "type": "FEATURE",
            "title": "Valid feature",
            "status": "ACTIVE",
            "deliverable": {
                "type": "code",
                "path": "src/utils.py",
            },
        }
        wo_path.write_text(yaml.dump(wo_data))

        deliverable = tmp_path / "src" / "utils.py"
        deliverable.parent.mkdir()
        deliverable.write_text("def helper():\n    return 42\n")

        # Create companion test file
        test_file = tmp_path / "tests" / "test_utils.py"
        test_file.parent.mkdir()
        test_file.write_text("from src.utils import helper\ndef test_helper():\n    assert helper() == 42\n")

        qa_dir = tmp_path / ".sync" / "qa" / "verdicts"
        qa_dir.mkdir(parents=True)
        verdict = qa_dir / "WO-003_verdict.md"
        verdict.write_text("# Verdict: APPROVED\n")

        gate = D024Gate()
        decision = gate.evaluate_work_order(tmp_path, "WO-003")

        assert decision.passed

    def test_d024_tracks_verification_hash(self, tmp_path):
        """D024 should track content hash in evidence."""
        from validators.harness.d024_gate import D024Gate
        import yaml

        sync_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        sync_dir.mkdir(parents=True)

        wo_path = sync_dir / "WO-004.yaml"
        wo_data = {
            "id": "WO-004",
            "type": "FEATURE",
            "title": "Test hash tracking",
            "status": "ACTIVE",
            "deliverable": {"type": "code", "path": "src/test.py"},
        }
        wo_path.write_text(yaml.dump(wo_data))

        deliverable = tmp_path / "src" / "test.py"
        deliverable.parent.mkdir()
        deliverable.write_text("x = 1\n")

        qa_dir = tmp_path / ".sync" / "qa" / "verdicts"
        qa_dir.mkdir(parents=True)
        verdict = qa_dir / "WO-004_verdict.md"
        verdict.write_text("# Verdict: APPROVED\n")

        gate = D024Gate()
        decision = gate.evaluate_work_order(tmp_path, "WO-004")

        assert "verification_hash" in decision.evidence
        assert decision.evidence["verification_hash"] is not None
        assert len(decision.evidence["verification_hash"]) > 0

    def test_d024_empty_deliverable_fails(self, tmp_path):
        """D024 should block empty deliverables."""
        from validators.harness.d024_gate import D024Gate
        import yaml

        sync_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        sync_dir.mkdir(parents=True)

        wo_path = sync_dir / "WO-005.yaml"
        wo_data = {
            "id": "WO-005",
            "type": "FEATURE",
            "title": "Test empty",
            "status": "ACTIVE",
            "deliverable": {"type": "code", "path": "src/empty.py"},
        }
        wo_path.write_text(yaml.dump(wo_data))

        deliverable = tmp_path / "src" / "empty.py"
        deliverable.parent.mkdir()
        deliverable.write_text("")

        qa_dir = tmp_path / ".sync" / "qa" / "verdicts"
        qa_dir.mkdir(parents=True)
        verdict = qa_dir / "WO-005_verdict.md"
        verdict.write_text("# Verdict: APPROVED\n")

        gate = D024Gate()
        decision = gate.evaluate_work_order(tmp_path, "WO-005")

        assert not decision.passed
        assert "empty" in decision.reason.lower() or "empty" in decision.evidence.get("verification_checks", [{}])[0].get("message", "").lower()
