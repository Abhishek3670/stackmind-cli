"""Unit and integration tests for Phase P3 Authentic Verification and Evidence Capture (WO-004)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from validators.experience.models import (
    ExperienceAction,
    ExperienceObservation,
    ExperienceRecord,
    ExperienceVerification,
)
from validators.harness.snapshot import TrustLevel, VerificationDimensions, WorkspaceDiff
from validators.kernel import (
    AgentContract,
    AuthenticEvidenceTracer,
    AuthenticObservation,
    ContractNormalizer,
    EligibilityDecision,
    ExperienceEligibilityGate,
    SandboxCanaryVerifier,
    ScratchWorkspace,
    derive_verification_dimensions,
)
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)



def _create_test_workspace(tmp_path):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "base.txt").write_text("base content", encoding="utf-8")
    workspace = ScratchWorkspace.create(authoritative, "attempt-p3")
    return authoritative, workspace


def test_authentic_observation_capture_file_write_and_diff(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)
    tracer = AuthenticEvidenceTracer(workspace)

    # 1. Initial write
    obs1 = tracer.trace_write(
        "service.py",
        "def compute():\n    return 42\n",
        operation_id="op-write-1",
    )
    assert obs1.operation_type == "write_file"
    assert obs1.exit_code == 0
    assert obs1.duration_ms >= 1
    assert obs1.files_touched == ("service.py",)
    assert obs1.before_hashes["service.py"] == ""
    assert len(obs1.after_hashes["service.py"]) == 64
    assert "+def compute():" in obs1.diff
    assert obs1.file_snapshots[0].status == "added"

    # 2. Modify write
    obs2 = tracer.trace_write(
        "service.py",
        "def compute():\n    return 84\n",
        operation_id="op-write-2",
    )
    assert obs2.file_snapshots[0].status == "modified"
    assert obs2.before_hashes["service.py"] == obs1.after_hashes["service.py"]
    assert "-    return 42" in obs2.diff
    assert "+    return 84" in obs2.diff
    assert len(tracer.observations) == 2


def test_authentic_observation_command_execution(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)
    tracer = AuthenticEvidenceTracer(workspace)

    # Command output telemetry
    obs_cmd = tracer.trace_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello-out\\n'); sys.stderr.write('hello-err\\n')"],
        operation_id="op-cmd-1",
    )
    assert obs_cmd.exit_code == 0
    assert "hello-out" in obs_cmd.stdout
    assert "hello-err" in obs_cmd.stderr
    assert obs_cmd.duration_ms >= 1
    assert obs_cmd.verified is True

    # Escape attempt containment
    obs_escape = tracer.trace_command(
        [sys.executable, "-c", "print('escape')", "../outside"],
        operation_id="op-cmd-escape",
    )
    assert obs_escape.contract_authorized is False
    assert obs_escape.exit_code == -1
    assert "WorkspaceEscapeError" in obs_escape.stderr
    assert obs_escape.is_error is True


def test_sandbox_canary_verifier(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)

    valid_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("test_auth", 1),
        name="test_auth",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Authentic canary skill",
        applicability=SkillApplicability(preconditions=("Service online",), target_modules=("auth/*",)),
        steps=(
            SkillStep(step_index=1, action="Verify python", tool="python", command_template="python -c 'import sys'"),
        ),
        provenance=SkillProvenance(
            source_experience_ids=(),
            created_at="2026-09-07T19:40:00Z",
            author_agent="codex",
        ),
    )


    # Valid canary run
    res_valid = SandboxCanaryVerifier.verify(
        valid_skill,
        workspace.root,
        commands=[[sys.executable, "-c", "import sys; sys.exit(0)"]],
        workspace=workspace,
    )
    assert res_valid.passed is True
    assert res_valid.details["boundary_verified"] is True
    assert res_valid.details["sandbox_commands_executed"] == 1

    # Failing command canary run
    res_fail = SandboxCanaryVerifier.verify(
        valid_skill,
        workspace.root,
        commands=[[sys.executable, "-c", "import sys; sys.exit(2)"]],
        workspace=workspace,
    )
    assert res_fail.passed is False
    assert "exit code 2" in res_fail.messages[0]


def test_evidence_derived_dimensions(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)
    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-004",
        "scope": {
            "allow": ["workspace/**"],
            "deny": ["workspace/forbidden/**"],
            "write": "read-write",
        },
    })

    tracer = AuthenticEvidenceTracer(workspace)
    tracer.trace_write("app.py", "x = 10\n")
    tracer.trace_command([sys.executable, "-c", "print('test ok')"])

    dims = derive_verification_dimensions(tracer.observations, contract, workspace.root)
    assert dims.all_passed is True
    assert dims.scope_verified is True
    assert dims.state_verified is True
    assert dims.code_verified is True
    assert dims.behavioral_verified is True
    assert dims.security_verified is True
    assert dims.outcome_verified is True

    # Test scope failure on denied write
    tracer_denied = AuthenticEvidenceTracer(workspace)
    tracer_denied.trace_write("forbidden/secret.py", "leak", contract_authorized=False, contract_reason="denied")
    dims_denied = derive_verification_dimensions(tracer_denied.observations, contract, workspace.root)
    assert dims_denied.scope_verified is False
    assert dims_denied.security_verified is False
    assert dims_denied.all_passed is False

    # Test syntax failure
    tracer_syntax = AuthenticEvidenceTracer(workspace)
    tracer_syntax.trace_write("broken.py", "def unclosed_syntax(")
    dims_syntax = derive_verification_dimensions(tracer_syntax.observations, contract, workspace.root)
    assert dims_syntax.code_verified is False


def test_experience_eligibility_gate(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)
    tracer = AuthenticEvidenceTracer(workspace)
    tracer.trace_write("main.py", "print('hello')\n")
    tracer.trace_command([sys.executable, "-c", "print('pass')"])

    dims = derive_verification_dimensions(tracer.observations, workspace_root=workspace.root)

    # 1. Everything clean -> LEARNING_ELIGIBLE
    decision_pass = ExperienceEligibilityGate.evaluate(
        tracer.observations,
        dims,
        canary_passed=True,
        has_blockers=False,
    )
    assert decision_pass.eligible is True
    assert decision_pass.trust_level == TrustLevel.LEARNING_ELIGIBLE
    assert len(decision_pass.reasons) == 0
    assert decision_pass.authentic_evidence_count == 2

    # 2. Canary failure -> Not eligible
    decision_canary_fail = ExperienceEligibilityGate.evaluate(
        tracer.observations,
        dims,
        canary_passed=False,
        has_blockers=False,
    )
    assert decision_canary_fail.eligible is False
    assert decision_canary_fail.trust_level == TrustLevel.VERIFIED
    assert any("canary" in r for r in decision_canary_fail.reasons)


def test_end_to_end_trace_to_experience_record(tmp_path):
    _, workspace = _create_test_workspace(tmp_path)
    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-004",
        "scope": {"allow": ["workspace/**"], "write": "read-write"},
    })

    tracer = AuthenticEvidenceTracer(workspace)
    obs1 = tracer.trace_write("calc.py", "def add(a, b):\n    return a + b\n")
    obs2 = tracer.trace_command([sys.executable, "-c", "import calc; assert calc.add(2, 3) == 5; print('OK')"])

    dims = derive_verification_dimensions(tracer.observations, contract, workspace.root)
    assert dims.all_passed is True

    # Run authentic canary
    canary_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("calc_skill", 1),
        name="calc_skill",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Calc skill",
        applicability=SkillApplicability(preconditions=("Python ready",), target_modules=("calc*",)),
        steps=(SkillStep(step_index=1, action="Verify", tool="python", command_template="python -c 'import sys'"),),
        provenance=SkillProvenance(
            source_experience_ids=(),
            created_at="2026-09-07T19:40:00Z",
            author_agent="codex",
        ),
    )

    canary_res = SandboxCanaryVerifier.verify(canary_skill, workspace.root, workspace=workspace)
    assert canary_res.passed is True

    eligibility = ExperienceEligibilityGate.evaluate(
        tracer.observations,
        dims,
        canary_passed=canary_res.passed,
        has_blockers=False,
    )
    assert eligibility.eligible is True

    # Mint and validate authentic ExperienceRecord
    diff = WorkspaceDiff(added=("calc.py",))
    actions = (
        ExperienceAction(tool="write_file", command_or_symbol="calc.py", input_summary=obs1.stdout, timestamp=obs1.recorded_at),
        ExperienceAction(tool="run_command", command_or_symbol=str(obs2.command), input_summary=obs2.stdout, timestamp=obs2.recorded_at),
    )
    observations = (
        ExperienceObservation(output_summary=obs1.stdout, exit_code=obs1.exit_code, is_error=obs1.is_error),
        ExperienceObservation(output_summary=obs2.stdout, exit_code=obs2.exit_code, is_error=obs2.is_error),
    )

    record = ExperienceRecord(
        experience_id=ExperienceRecord.mint_id("task-calc", "2026-09-07T19:40:00Z"),
        task_id="task-calc-1",
        agent_id="codex",
        task_signature="task-calc",
        environment={"runtime": "3.1.0"},
        verification=ExperienceVerification(
            dimensions=dims,
            observed_diff=diff,
            contract_id="WO-004",
        ),
        trust_level=eligibility.trust_level,
        learning_eligible=eligibility.eligible,
        outcome="completed",
        recorded_at="2026-09-07T19:40:00Z",
        actions=actions,
        observations=observations,
    )

    record.validate_schema()
    assert record.learning_eligible is True
    assert record.trust_level == TrustLevel.LEARNING_ELIGIBLE
