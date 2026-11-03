"""Tests for session 4a4e852a fixes:
1. Discovering JSON QA defect verdicts in .sync/reviews/qa/
2. Preserving delegated repair work orders during plan reconciliation
3. Restoring erroneously cancelled orphan delegated work orders
4. Final review gate routing QA failures to developers instead of blocking
"""

import json
from pathlib import Path
import pytest
import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class TestJsonQaDefectDiscovery:
    def test_discovers_json_qa_defect_in_reviews_qa(self, tmp_path: Path):
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-001", "Build calculator", tmp_path, "sess-001")
        state.phase = Phase.EXECUTING
        state.worker_wo_ids = ["WO-006", "WO-009"]
        state.completed_wo_ids = ["WO-006"]

        # WO-006 owns index.html
        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-006.yaml",
            {
                "id": "WO-006",
                "type": "FEATURE",
                "title": "Frontend Calculator",
                "status": "COMPLETED",
                "assigned_agents": ["gemini"],
                "deliverable": {"type": "code", "path": "index.html"},
            },
        )
        (tmp_path / "index.html").write_text("<html>no backspace</html>", encoding="utf-8")

        # QA verdict saved in .sync/reviews/qa/WO-009.json
        qa_verdict = {
            "schema": "stackmind.qa-verdict.v1",
            "work_order_id": "WO-009",
            "verdict": "FAIL",
            "findings": [
                {
                    "severity": "major",
                    "detail": "index.html has no backspace control or handler",
                }
            ],
            "required_action": "Developer must add a backspace button in index.html",
        }
        _write_json(tmp_path / ".sync" / "reviews" / "qa" / "WO-009.json", qa_verdict)

        hits = supervisor._find_qa_defect_verdicts(tmp_path, "WO-009", state=state)
        assert len(hits) >= 1
        target_wo, finding_text, verdict_path = hits[0]
        assert target_wo == "WO-006"
        assert "index.html" in finding_text

    def test_routes_qa_json_defect_to_developer(self, tmp_path: Path):
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-001", "Build calculator", tmp_path, "sess-001")
        state.phase = Phase.EXECUTING
        state.worker_wo_ids = ["WO-006", "WO-009"]
        state.completed_wo_ids = ["WO-006"]

        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-006.yaml",
            {
                "id": "WO-006",
                "type": "FEATURE",
                "title": "Frontend Calculator",
                "status": "COMPLETED",
                "assigned_agents": ["gemini"],
                "deliverable": {"type": "code", "path": "index.html"},
            },
        )
        (tmp_path / "index.html").write_text("<html>no backspace</html>", encoding="utf-8")

        qa_verdict = {
            "schema": "stackmind.qa-verdict.v1",
            "work_order_id": "WO-009",
            "verdict": "FAIL",
            "findings": [{"severity": "major", "detail": "index.html has no backspace control"}],
            "required_action": "Developer must add a backspace button in index.html",
        }
        _write_json(tmp_path / ".sync" / "reviews" / "qa" / "WO-009.json", qa_verdict)

        qa_op = mock_mgr.start_turn("sess-001", "Run QA", work_order_id="WO-009", agent_id="gemma")
        mock_mgr.complete_operation(qa_op["operation_id"], "BLOCKED")

        routed = supervisor._route_qa_defect_to_developers(
            state, tmp_path, "WO-009", qa_op, "Test failed", ""
        )
        assert routed == AdvanceResult.WAITING_FOR_OPERATION
        assert "WO-006" not in state.completed_wo_ids
        assert "WO-006" in state.qa_rework_targets.get("WO-009", [])
        assert state.qa_reworks.get("WO-006") == 1


PLAN_TEXT = """# Project Plan
## Current Architecture
Setup architecture
## Proposed Changes
Proposed changes
## Milestones
- [ ] Milestone 1: Setup (Agent: codex)
  - [ ] Task 1.1: Initialize project
"""


class TestDelegatedWorkOrderReconciliation:
    def test_preserves_architect_delegated_work_order(self, tmp_path: Path):
        (tmp_path / "PLAN.md").write_text(PLAN_TEXT, encoding="utf-8")
        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-016.yaml",
            {
                "id": "WO-016",
                "type": "FEATURE",
                "title": "Backend: Repair the package.json deliverable",
                "status": "ACTIVE",
                "synthesized_by": "architect_delegation",
                "assigned_agents": ["codex"],
                "deliverable": {"type": "code", "path": "package.json"},
            },
        )

        reconcile_active_work_orders_with_plan(tmp_path)

        wo_data = yaml.safe_load(
            (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-016.yaml").read_text(encoding="utf-8")
        )
        assert wo_data["status"] == "ACTIVE"
        assert "Orphaned" not in str(wo_data.get("cancellation_reason", ""))

    def test_revives_erroneously_cancelled_delegated_work_order(self, tmp_path: Path):
        (tmp_path / "PLAN.md").write_text(PLAN_TEXT, encoding="utf-8")
        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-016.yaml",
            {
                "id": "WO-016",
                "type": "FEATURE",
                "title": "Backend: Repair the package.json deliverable",
                "status": "CANCELLED",
                "cancellation_reason": "Orphaned work order not present in current PLAN.md",
                "error": "Orphaned work order not present in current PLAN.md",
                "synthesized_by": "architect_delegation",
                "assigned_agents": ["codex"],
                "deliverable": {"type": "code", "path": "package.json"},
            },
        )

        reconcile_active_work_orders_with_plan(tmp_path)

        wo_data = yaml.safe_load(
            (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-016.yaml").read_text(encoding="utf-8")
        )
        assert wo_data["status"] == "ACTIVE"
        assert "cancellation_reason" not in wo_data


class TestFinalReviewAndResumeFixes:
    def test_final_review_routes_qa_failures_to_executing_rework(self, tmp_path: Path):
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-001", "Build calculator", tmp_path, "sess-001")
        state.phase = Phase.FINAL_REVIEW
        state.worker_wo_ids = ["WO-006"]
        state.completed_wo_ids = ["WO-006"]

        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-006.yaml",
            {
                "id": "WO-006",
                "type": "FEATURE",
                "title": "Frontend Calculator",
                "status": "COMPLETED",
                "assigned_agents": ["gemini"],
                "deliverable": {"type": "code", "path": "index.html"},
            },
        )
        (tmp_path / "index.html").write_text("<html>no backspace</html>", encoding="utf-8")
        (tmp_path / "PLAN.md").write_text(PLAN_TEXT, encoding="utf-8")

        res = supervisor._advance_final_review(state)
        assert res == AdvanceResult.TRANSITIONED
        assert state.phase in (Phase.EXECUTING, Phase.DISPATCHING)
        assert "WO-006" not in state.completed_wo_ids
        assert state.qa_reworks.get("WO-006", 0) >= 1
        assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-006.yaml").is_file()

    def test_human_resume_unarchives_blocked_work_orders_and_clears_blockers(self, tmp_path: Path):
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-001", "Build calculator", tmp_path, "sess-001")
        state.phase = Phase.BLOCKED
        state.worker_wo_ids = ["WO-004"]
        state.blocked_wo_ids = ["WO-004"]
        state.worker_blockers = {"WO-004": {"failure_code": "TEST_FAILED", "canonical_message": "failed"}}
        state.authoring_repair_attempts = 3
        state.final_prerequisite_rounds = 2

        _write_yaml(
            tmp_path / ".sync" / "work-orders" / "BLOCKED" / "WO-004.yaml",
            {
                "id": "WO-004",
                "type": "VALIDATION",
                "title": "QA verification suite",
                "status": "BLOCKED",
                "assigned_agents": ["gemma"],
                "deliverable": {"type": "code", "path": "tests/test_qa.py"},
            },
        )

        res = supervisor.resume_run_with_human_override(state, reason="Operator unblocking run")
        assert res == AdvanceResult.TRANSITIONED
        assert state.phase == Phase.DISPATCHING
        assert state.blocked_wo_ids == []
        assert state.worker_blockers == {}
        assert state.authoring_repair_attempts == 0
        assert state.final_prerequisite_rounds == 0
        assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-004.yaml").is_file()
        assert not (tmp_path / ".sync" / "work-orders" / "BLOCKED" / "WO-004.yaml").is_file()
