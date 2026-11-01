"""SDP-FIX-009-FINAL — Supervisor-level release enforcement tests.

Validates that StackMind's supervisor:
1. Enforces delivery-integrity at actual release boundaries (`gitops._advance_gitops`, `phases._advance_complete`, `phases._advance_product_ready`).
2. Fails closed deterministically when planning, artifacts, QA, integration, or evidence currency fail.
3. Persists structured blockers and evidence SHA-256 in supervisor RunState.
4. Prevents TOCTOU vulnerabilities (rejects release if evidence changes post-evaluation).
5. Surfaces authoritative delivery state via TUI commands (`:delivery`, `:status`).
6. Preserves blocked/ready status across supervisor restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from cli.tui.commands import dispatch_delivery_command, get_status_str
from cli.tui.state import AutonomousDeliveryState
from validators.harness import delivery_integrity as di
from validators.harness import review_gate
from validators.harness.integration_gate import IntegrationCheck, IntegrationGateDecision
from validators.kernel.daemon.supervisor import AdvanceResult, LifecycleSupervisor, Phase, RunState


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_plan(ws: Path, criteria=("User can register",)) -> None:
    text = "# Delivery Plan\n\n## Current Architecture\n- Core system design\n\n## Acceptance Criteria\n" + "".join(f"- {c}\n" for c in criteria)
    text += "\n## Milestones & Roadmap\n- [ ] Milestone 1: Core setup\n"
    (ws / "PLAN.md").write_text(text, encoding="utf-8")


def write_wo(
    ws: Path,
    wo_id: str,
    *,
    status: str = "ACTIVE",
    agent: str = "codex",
    deliverable: str = "src/app.py",
    criteria: list[str] | None = None,
    deps: list[str] | None = None,
    wo_type: str = "FEATURE",
    title: str | None = None,
) -> dict:
    bucket = "ACTIVE" if status == "ACTIVE" else status
    directory = ws / ".sync" / "work-orders" / bucket
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": wo_id,
        "type": wo_type,
        "title": title or f"{wo_id} implementation",
        "status": status,
        "assigned_agents": [agent],
        "dependencies": deps or [],
        "deliverable": {"type": "code", "path": deliverable, "description": "deliverable"},
        "acceptance_criteria": criteria or [f"{wo_id} works"],
    }
    (directory / f"{wo_id}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return data


def write_contract(ws: Path, wo_id: str, agent: str = "codex") -> None:
    directory = ws / ".sync" / "contracts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{wo_id}.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "scope": {"allow": [{"module": "**"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 20},
    }), encoding="utf-8")


def write_file(ws: Path, rel: str, content: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_qa(ws: Path, wo_id: str, verdict: str = "PASS", outcome: str = "PASS") -> dict:
    data = {
        "schema": review_gate.QA_VERDICT_SCHEMA,
        "work_order_id": wo_id,
        "verdict": verdict,
        "criteria": [{"id": "AC1", "outcome": outcome}],
        "findings": [],
        "required_actions": [],
        "evidence": [{"kind": "test", "command": "pytest -q tests/", "exit_code": 0, "passed": verdict == "PASS"}],
        "subject_hash": review_gate.subject_hash(ws, wo_id),
    }
    (ws / ".sync" / "reviews" / "qa").mkdir(parents=True, exist_ok=True)
    review_gate.persist_qa_verdict(ws, data)
    return data


def setup_clean_workspace(ws: Path) -> None:
    write_plan(ws, criteria=["User can register"])
    write_wo(ws, "WO-001", deliverable="src/app.py", criteria=["User can register"])
    write_wo(ws, "WO-002", agent="gemma", wo_type="QA", deliverable="tests/test_app.py", criteria=["Automated tests pass"])
    write_wo(ws, "WO-003", agent="local-llm", wo_type="GITOPS", deliverable="VERSION.md", criteria=["Release documented"])
    write_file(ws, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_file(ws, "tests/test_app.py", "def test_register():\n    assert True\n")
    write_file(ws, "VERSION.md", "1.0.0\n")
    write_file(ws, "README.md", "# Application\n\n## Quickstart\npython app.py\n")
    write_file(ws, "CHANGELOG.md", "# Changelog\n\n- initial release\n")
    write_contract(ws, "WO-001")
    write_qa(ws, "WO-001", verdict="PASS")


def patch_integration(monkeypatch, *, passed: bool = True, message: str = "integration evidence complete") -> None:
    def fake(self, state=None, integration_op=None, extra_telemetry=None):
        check = IntegrationCheck(
            name="deterministic",
            passed=passed,
            status="passed" if passed else "failed",
            message=message,
        )
        return IntegrationGateDecision(
            passed=passed,
            issues=() if passed else (message,),
            checks=(check,),
        )

    monkeypatch.setattr(di.IntegrationGate, "evaluate", fake)


def make_run_state(ws: Path, phase: Phase = Phase.GITOPS, gitops_op_id: str | None = "op-gitops-001") -> RunState:
    state = RunState(
        run_id="run-test009",
        product_goal="Build login application",
        workspace=str(ws),
        session_id="session-test009",
        phase=phase,
        planning_wo_id="WO-001",
        completed_wo_ids=["WO-001", "WO-002"],
        gitops_wo_id="WO-003",
        gitops_operation_id=gitops_op_id,
    )
    return state


def make_supervisor() -> LifecycleSupervisor:
    manager = MagicMock()
    supervisor = LifecycleSupervisor(manager=manager)
    ops = {
        "op-gitops-001": {
            "operation_id": "op-gitops-001",
            "status": "COMPLETED",
            "result": {"status": "completed"},
        }
    }
    supervisor._get_operation = lambda op_id: ops.get(op_id)
    return supervisor


# ─────────────────────────────────────────────────────────────────────────────
# 1. Supervisor GitOps Release Enforcement Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_gitops_release_blocked_when_planning_criteria_unmapped(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Add an unmapped acceptance criterion to PLAN.md
    write_plan(tmp_path, criteria=["User can register", "Admin can delete user"])
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_PLANNING_GATE" for b in state.delivery_blockers)


def test_gitops_release_blocked_when_artifact_empty(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Make deliverable artifact empty (0 bytes)
    write_file(tmp_path, "src/app.py", "")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_ARTIFACTS_GATE" for b in state.delivery_blockers)


def test_gitops_release_blocked_when_qa_verdict_failed(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Record QA FAIL verdict
    write_qa(tmp_path, "WO-001", verdict="FAIL", outcome="FAIL")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_QA_GATE" for b in state.delivery_blockers)


def test_gitops_release_blocked_when_integration_fails(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    patch_integration(monkeypatch, passed=False, message="Build failed during integration check")

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_INTEGRATION_GATE" for b in state.delivery_blockers)


def test_gitops_advances_and_commits_when_all_delivery_checks_pass(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    patch_integration(monkeypatch, passed=True)

    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("validators.kernel.daemon.supervisor.gitops.create_gitops_commit", lambda **kwargs: "commit-sha-009")
    monkeypatch.setattr("validators.kernel.sync_git.commit_sync_governance", lambda *args, **kwargs: None)
    monkeypatch.setattr(LifecycleSupervisor, "_final_acceptance_status", lambda self, state, ws: (True, {}, "ok"))

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.COMPLETE
    assert state.delivery_decision_passed is True
    assert len(state.delivery_blockers) == 0
    assert state.release_commit_sha == "commit-sha-009"


# ─────────────────────────────────────────────────────────────────────────────
# 2. TOCTOU & Evidence Freshness Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_toctou_tampering_between_evaluation_and_commit_triggers_rejection(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    patch_integration(monkeypatch, passed=True)

    # Stub final acceptance pass
    monkeypatch.setattr(LifecycleSupervisor, "_final_acceptance_status", lambda self, state, ws: (True, {}, "ok"))

    # Hook into decision_is_current to simulate file modification right before TOCTOU check
    original_decision_is_current = di.decision_is_current
    def tampered_decision_is_current(ws, state, decision, **kwargs):
        # Tamper with workspace
        write_file(tmp_path, "src/app.py", "def tampered(): pass\n")
        return original_decision_is_current(ws, state, decision, **kwargs)

    monkeypatch.setattr("validators.harness.delivery_integrity.decision_is_current", tampered_decision_is_current)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "RELEASE_EVIDENCE_CHANGED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "STALE_EVIDENCE" for b in state.delivery_blockers)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Terminal Completion & Product Ready Boundary Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_advance_complete_fails_closed_when_delivery_integrity_violated(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Corrupt QA verdict
    write_qa(tmp_path, "WO-001", verdict="FAIL")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.COMPLETE)

    result = supervisor._advance_complete(state)

    assert result == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert "DELIVERY_INTEGRITY_VIOLATION" in (state.error or "")
    assert state.delivery_decision_passed is False


def test_advance_complete_succeeds_when_delivery_integrity_passed(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    patch_integration(monkeypatch, passed=True)
    monkeypatch.setattr(LifecycleSupervisor, "_final_acceptance_status", lambda self, state, ws: (True, {}, "ok"))

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.COMPLETE)

    result = supervisor._advance_complete(state)

    assert result == AdvanceResult.COMPLETE
    assert state.delivery_decision_passed is True


def test_advance_product_ready_blocks_when_final_acceptance_not_passed(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    monkeypatch.setattr(LifecycleSupervisor, "_final_acceptance_status", lambda self, state, ws: (False, {}, "Gemma rejected review"))

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.PRODUCT_READY)

    result = supervisor._advance_product_ready(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "FINAL_ACCEPTANCE_NOT_PASSED" in (state.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# 4. State Persistence, Restart & TUI Projection Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_run_state_serialization_preserves_delivery_fields() -> None:
    state = RunState(
        run_id="run-persist001",
        product_goal="Verify serialization",
        workspace="/test/ws",
        session_id="session-001",
        delivery_decision_passed=False,
        delivery_evidence_sha256="abcd1234efgh5678",
        delivery_evaluated_at="2026-10-31T18:00:00Z",
        delivery_blockers=[{
            "code": "QA_VERDICT_FAILED",
            "phase": "GITOPS",
            "affected_target": "WO-001",
            "requirement": "All QA verdicts must be PASS",
            "is_blocking": True,
            "remediation": "Re-run QA test suite",
        }],
    )
    d = state.to_dict()
    assert d["delivery_decision_passed"] is False
    assert d["delivery_evidence_sha256"] == "abcd1234efgh5678"
    assert len(d["delivery_blockers"]) == 1

    restored = RunState.from_dict(d)
    assert restored.delivery_decision_passed is False
    assert restored.delivery_evidence_sha256 == "abcd1234efgh5678"
    assert len(restored.delivery_blockers) == 1
    assert restored.delivery_blockers[0]["code"] == "QA_VERDICT_FAILED"


def test_tui_delivery_command_renders_read_only_snapshot(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    patch_integration(monkeypatch, passed=True)

    # Persist an authoritative decision snapshot
    state = make_run_state(tmp_path)
    decision = di.evaluate_release_readiness(tmp_path, state)
    di.persist_release_decision(tmp_path, decision, run_id=state.run_id, phase="GITOPS", source="test")

    session = {"session_id": "sess-tui-01", "workspace": str(tmp_path)}
    tui_state = AutonomousDeliveryState()

    adapter = MagicMock()
    client = MagicMock()

    # Invoke :delivery command
    out_session, consumed = dispatch_delivery_command(
        adapter,
        client,
        session,
        ":delivery",
        tui_state,
    )

    assert not consumed
    # Check messages in tui_state
    system_messages = [m.content for m in tui_state.messages if m.role == "system"]
    assert any("DELIVERY INTEGRITY" in msg for msg in system_messages)
    assert any("GATES" in msg for msg in system_messages)


def test_tui_status_displays_delivery_readiness_summary(tmp_path: Path) -> None:
    # 1. Without decision snapshot
    session = {"session_id": "sess-status-01", "workspace": str(tmp_path)}
    status_str = get_status_str(session)
    assert "Delivery:" not in status_str

    # 2. With passing decision snapshot
    snap_data = {
        "schema": di.RELEASE_DECISION_SCHEMA,
        "run_id": "run-01",
        "phase": "GITOPS",
        "passed": True,
        "blockers": [],
    }
    p = tmp_path / di.RELEASE_DECISION_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap_data), encoding="utf-8")

    status_str = get_status_str(session)
    assert "Delivery: READY" in status_str

    # 3. With blocked decision snapshot
    snap_data["passed"] = False
    snap_data["blockers"] = [{"code": "QA_VERDICT_FAILED"}]
    p.write_text(json.dumps(snap_data), encoding="utf-8")

    status_str = get_status_str(session)
    assert "Delivery: BLOCKED (1 open blocker)" in status_str


# ─────────────────────────────────────────────────────────────────────────────
# 5. Additional Detailed Governance & Structural Invariant Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_duplicate_work_orders_block_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Duplicate WO-001 into COMPLETED as well as ACTIVE
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)
    (completed_dir / "WO-001.yaml").write_text((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False


def test_dependency_cycle_blocks_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Create cycle: WO-001 depends on WO-002, WO-002 depends on WO-001
    write_wo(tmp_path, "WO-001", deliverable="src/app.py", criteria=["User can register"], deps=["WO-002"])
    write_wo(tmp_path, "WO-002", agent="gemma", wo_type="QA", deliverable="tests/test_app.py", criteria=["Automated tests pass"], deps=["WO-001"])
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False


def test_missing_required_artifact_blocks_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Delete deliverable artifact
    (tmp_path / "src" / "app.py").unlink()
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_ARTIFACTS_GATE" for b in state.delivery_blockers)


def test_artifact_traversal_outside_workspace_rejected(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Deliverable attempts path traversal outside workspace
    write_wo(tmp_path, "WO-001", deliverable="../outside_secret.py", criteria=["User can register"])
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False


def test_qa_verdict_not_tested_blocks_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    write_qa(tmp_path, "WO-001", verdict="NOT_TESTED", outcome="NOT_TESTED")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_QA_GATE" for b in state.delivery_blockers)


def test_qa_verdict_inconclusive_blocks_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    write_qa(tmp_path, "WO-001", verdict="INCONCLUSIVE", outcome="INCONCLUSIVE")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_QA_GATE" for b in state.delivery_blockers)


def test_qa_verdict_blocked_blocks_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    write_qa(tmp_path, "WO-001", verdict="BLOCKED", outcome="BLOCKED")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_QA_GATE" for b in state.delivery_blockers)


def test_post_qa_artifact_modification_invalidates_release(tmp_path: Path, monkeypatch) -> None:
    setup_clean_workspace(tmp_path)
    # Sign QA verdict over current artifact
    write_qa(tmp_path, "WO-001", verdict="PASS", outcome="PASS")
    # Modify artifact post-QA
    write_file(tmp_path, "src/app.py", "# Malicious / unsanctioned edit\ndef backdoor(): pass\n")
    patch_integration(monkeypatch, passed=True)

    supervisor = make_supervisor()
    state = make_run_state(tmp_path, Phase.GITOPS)

    result = supervisor._advance_gitops(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "DELIVERY_INTEGRITY_BLOCKED" in (state.error or "")
    assert state.delivery_decision_passed is False
    assert any(b["code"] == "DELIVERY_QA_GATE" for b in state.delivery_blockers)


def test_repeated_evaluation_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    setup_clean_workspace(tmp_path)
    state = make_run_state(tmp_path)

    d1 = di.evaluate_release_readiness(tmp_path, state, require_documentation=True)
    d2 = di.evaluate_release_readiness(tmp_path, state, require_documentation=True)
    d3 = di.evaluate_release_readiness(tmp_path, state, require_documentation=True)

    assert d1.passed == d2.passed == d3.passed
    assert d1.issues == d2.issues == d3.issues
    assert d1.evidence.get("evidence_sha256") == d2.evidence.get("evidence_sha256") == d3.evidence.get("evidence_sha256")
    assert di.structured_blockers(d1) == di.structured_blockers(d2) == di.structured_blockers(d3)
