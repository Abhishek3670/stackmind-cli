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


def test_integration_gate_skips_internal_sync_deliverables(tmp_path: Path):
    from validators.harness.integration_gate import IntegrationGate

    gate = IntegrationGate(tmp_path)
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    act_dir.mkdir(parents=True, exist_ok=True)

    wo15 = {
        "id": "WO-015",
        "title": "Repair WO-002",
        "deliverable": {"type": "config", "path": ".sync/work-orders/ACTIVE/WO-002.yaml"},
    }
    (act_dir / "WO-015.yaml").write_text(yaml.safe_dump(wo15), encoding="utf-8")

    check, issues, blockers, ev = gate._check_artifacts_and_freshness(None, None, None)
    # WO-015's .sync deliverable must be skipped, not failed as missing
    assert not any("WO-015" in i for i in issues)
    assert not any("WO-015" in b.get("finding", "") for b in blockers)


def test_superseded_dependency_self_supersession(tmp_path: Path):
    from validators.kernel.daemon.supervisor.dependencies import validate_dependency_graph, _successor_for

    sync = tmp_path / ".sync" / "work-orders"
    active_dir = sync / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo4 = {
        "id": "WO-004",
        "status": "CANCELLED",
        "cancellation_reason": "Superseded by completed WO-005 (deliverable 'tests/test_qa_verification.py' already completed)",
        "dependencies": ["WO-003"],
    }
    wo5 = {
        "id": "WO-005",
        "status": "ACTIVE",
        "dependencies": ["WO-004"],
    }
    (active_dir / "WO-004.yaml").write_text(yaml.safe_dump(wo4), encoding="utf-8")
    (active_dir / "WO-005.yaml").write_text(yaml.safe_dump(wo5), encoding="utf-8")

    issues = validate_dependency_graph(tmp_path)
    # Self-supersession (WO-005 superseded WO-004) must not trigger SUPERSEDED_DEPENDENCY
    assert not any(i.code == "SUPERSEDED_DEPENDENCY" and i.work_order_id == "WO-005" for i in issues)


def test_completed_work_order_ignores_cancelled_dependency(tmp_path: Path):
    from validators.kernel.daemon.supervisor.dependencies import validate_dependency_graph

    sync = tmp_path / ".sync" / "work-orders"
    act_dir = sync / "ACTIVE"
    comp_dir = sync / "COMPLETED"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)

    wo4 = {
        "id": "WO-004",
        "status": "CANCELLED",
        "cancellation_reason": "Superseded by completed WO-005",
        "dependencies": [],
    }
    wo5 = {
        "id": "WO-005",
        "status": "COMPLETED",
        "dependencies": ["WO-004"],
    }
    (act_dir / "WO-004.yaml").write_text(yaml.safe_dump(wo4), encoding="utf-8")
    (comp_dir / "WO-005.yaml").write_text(yaml.safe_dump(wo5), encoding="utf-8")

    issues = validate_dependency_graph(tmp_path)
    assert not any(i.code == "SUPERSEDED_DEPENDENCY" and i.work_order_id == "WO-005" for i in issues)


def test_outcome_verified_allows_blocked_planning_turn(tmp_path: Path):
    from validators.harness.runner.verification import _VerificationMixin
    from validators.harness.runner.models import HarnessTask, HarnessDecision
    from unittest.mock import MagicMock

    mixin = _VerificationMixin()
    mixin.project_path = tmp_path
    mixin.sync_path = tmp_path / ".sync"
    mixin.agent = "claude"

    task = HarnessTask(
        identifier="WO-000",
        kind="work_order",
        work_order_id="WO-000",
        title="System architecture and execution plan",
        deliverable_path=".sync/playbook/PLAN.md",
        deliverable_type="doc",
        path=mixin.sync_path / "work-orders" / "ACTIVE" / "WO-000.yaml",
        body="",
        query="",
    )

    dec_blocked = HarnessDecision(
        status="blocked",
        summary="Awaiting human clarification on 3D stack requirements",
        report_markdown="Awaiting clarification",
        blockers=("Clarification needed",),
        blocker_details=(),
        modified_files=(),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
        commands=(),
    )

    diff = MagicMock()
    diff.all_changed_files = ()

    dims = mixin._evaluate_verification_dimensions(
        task=task,
        decision=dec_blocked,
        diff=diff,
        before_snapshot=MagicMock(),
        after_snapshot=MagicMock(),
        staged_root=tmp_path,
        staged_errors=[],
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=False,
        tool_runtime=None,
    )
    # A blocked planning/architecture turn with valid blockers must not fail outcome_verified
    assert dims.outcome_verified is True


def test_planning_turn_synthesizes_plan_deliverable(tmp_path: Path):
    from validators.harness.runner.completion import _CompletionMixin
    from validators.harness.runner.models import HarnessTask, LLMRequest
    from unittest.mock import MagicMock

    mixin = _CompletionMixin()
    mixin.project_path = tmp_path
    mixin.sync_path = tmp_path / ".sync"
    mixin.agent = "claude"

    task = HarnessTask(
        identifier="WO-000",
        kind="work_order",
        work_order_id="WO-000",
        title="System architecture and execution plan",
        deliverable_path=".sync/playbook/PLAN.md",
        deliverable_type="doc",
        path=mixin.sync_path / "work-orders" / "ACTIVE" / "WO-000.yaml",
        body="",
        query="",
    )

    req = LLMRequest(
        agent="claude",
        session_count=1,
        task=task,
        context=MagicMock(),
        retrieval=MagicMock(),
    )

    # When model outputs raw markdown without JSON decision or write_file
    raw_markdown = "I propose a 3D mobile launch site using Three.js and GSAP."
    payload, written_files = mixin._extract_unstructured_fallback(
        request=req,
        raw_text=raw_markdown,
        tool_runtime=None,
        written_files=[],
    )

    assert payload.get("status") == "completed"
    assert (tmp_path / ".sync" / "playbook" / "PLAN.md").is_file()
    assert ".sync/playbook/PLAN.md" in payload.get("modified_files", [])


def test_resume_run_with_worker_wos_advances_to_dispatching(tmp_path: Path):
    from unittest.mock import MagicMock
    from validators.kernel.daemon.supervisor.models import RunState, Phase
    from validators.kernel.daemon.supervisor.phases import _PhasesMixin
    from validators.kernel.daemon.supervisor.controls import _ControlsMixin

    class DummySupervisor(_PhasesMixin, _ControlsMixin):
        def __init__(self):
            self.manager = MagicMock()
            self.manager.list_plans.return_value = []
            self.manager.get_operation.return_value = None

        def _transition(self, state, phase, **kwargs):
            state.phase = phase

    sup = DummySupervisor()
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "WO-001.yaml").write_text("id: WO-001\nstatus: ACTIVE\n", encoding="utf-8")

    state = RunState(
        run_id="run-test",
        workspace=str(tmp_path),
        session_id="sess-test",
        product_goal="Build test app",
        phase=Phase.PLANNING,
        planning_wo_id="WO-000",
        interrupted_phase="PLANNING",
        worker_wo_ids=["WO-001"],
    )

    # When advancing planning with worker WOs already present, advances directly to DISPATCHING
    res = sup._advance_planning(state)
    assert state.phase == Phase.DISPATCHING

    # Re-attest also honors DISPATCHING rather than reverting to stale interrupted_phase PLANNING
    sup.re_attest_run(state, human_signature="operator", confirmed_budget=100)
    assert state.phase == Phase.DISPATCHING





