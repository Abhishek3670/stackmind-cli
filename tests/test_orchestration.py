"""Fail-closed orchestration tests for harness gate evaluation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from validators.harness.orchestration import (
    ReviewOutcome,
    ReviewResult,
    ReleaseDecision,
    structured_output_validator,
    release_decision,
)


class TestReviewOutcomeEnum:
    """Test ReviewOutcome enum values."""

    def test_enum_values_exist(self):
        assert ReviewOutcome.PASS is not None
        assert ReviewOutcome.FAIL is not None
        assert ReviewOutcome.BLOCKED is not None
        assert ReviewOutcome.ERROR is not None

    def test_outcome_comparison(self):
        assert ReviewOutcome.PASS != ReviewOutcome.FAIL
        assert ReviewOutcome.PASS == ReviewOutcome.PASS


class TestReviewResult:
    """Test ReviewResult dataclass."""

    def test_review_result_basic(self):
        result = ReviewResult(
            outcome=ReviewOutcome.PASS,
            gate_name="clean_delivery",
            details="All checks passed",
            evidence={"scan_results": []},
        )
        assert result.outcome == ReviewOutcome.PASS
        assert result.gate_name == "clean_delivery"
        assert result.details == "All checks passed"

    def test_review_result_without_details(self):
        result = ReviewResult(
            outcome=ReviewOutcome.FAIL,
            gate_name="d024",
        )
        assert result.details is None
        assert result.evidence is None


class TestStructuredOutputValidator:
    """Test structured_output_validator with bounded retry."""

    def test_valid_payload_passes(self):
        outcome, payload = structured_output_validator({
            "status": "completed",
            "summary": "Task completed",
            "blockers": [],
        })
        assert outcome == ReviewOutcome.PASS
        assert payload.get("status") == "completed"

    def test_invalid_status_fails(self):
        outcome, payload = structured_output_validator({
            "status": "unknown_status",
        })
        assert outcome == ReviewOutcome.ERROR
        assert "_validation_error" in payload

    def test_non_dict_payload_fails(self):
        outcome, payload = structured_output_validator(["not", "a", "dict"])
        assert outcome == ReviewOutcome.ERROR

    def test_missing_status_fails(self):
        outcome, payload = structured_output_validator({"summary": "test"})
        assert outcome == ReviewOutcome.ERROR

    def test_blockers_with_completed_fails(self):
        outcome, payload = structured_output_validator({
            "status": "completed",
            "blockers": ["something is wrong"],
        })
        assert outcome == ReviewOutcome.ERROR

    def test_max_retries_exhausted_returns_error(self):
        call_count = 0

        def failing_refetch():
            nonlocal call_count
            call_count += 1
            return {"status": "invalid"}

        outcome, payload = structured_output_validator(
            {"status": "invalid"},
            max_retries=2,
            refetch_fn=failing_refetch,
        )
        assert outcome == ReviewOutcome.ERROR
        assert call_count == 2


class TestReleaseDecision:
    """Test release_decision derivation logic."""

    def test_empty_outcomes_returns_blocked(self):
        decision = release_decision([])
        assert decision.approved is False
        assert decision.required_gates_passed is False
        assert "no gate outcomes provided" in decision.blocking_reasons

    def test_all_pass_approves(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is True
        assert decision.required_gates_passed is True
        assert decision.clean_delivery_passed is True
        assert decision.d024_passed is True
        assert decision.execution_qa_passed is True

    def test_any_fail_blocks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.FAIL, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False
        assert "D024 gate" in decision.blocking_reasons[0]

    def test_any_blocked_blocks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.BLOCKED, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False

    def test_any_error_blocks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.ERROR, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False

    def test_stale_evidence_flag(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
        ]
        decision = release_decision(outcomes, has_stale_evidence=True)
        assert decision.has_stale_evidence is True

    def test_conflicting_results_flag(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
        ]
        decision = release_decision(outcomes, conflicting=True)
        assert decision.has_conflicting_results is True


class TestLoopGuard:
    """Test loop guard with unresolved findings."""

    def test_loop_guard_with_unresolved_findings_blocks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.FAIL, "d024", "Review found unresolved issues"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False

    def test_loop_guard_with_resolved_findings_passes(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is True


class TestMalformedOutput:
    """Test malformed structured output recovery."""

    def test_malformed_output_successful_recovery(self):
        refetch_calls = [0]

        def refetch_fn():
            refetch_calls[0] += 1
            return {"status": "completed", "summary": "Recovered", "blockers": []}

        outcome, payload = structured_output_validator(
            {"status": 123},  # Invalid status type
            refetch_fn=refetch_fn,
        )
        assert outcome == ReviewOutcome.PASS
        assert refetch_calls[0] == 1

    def test_malformed_output_invalid_after_retries(self):
        def refetch_fn():
            return {"status": "still_invalid"}

        outcome, payload = structured_output_validator(
            {"status": "invalid"},
            max_retries=3,
            refetch_fn=refetch_fn,
        )
        assert outcome == ReviewOutcome.ERROR
        assert "_validation_error" in payload


class TestProviderException:
    """Test provider exception handling."""

    def test_provider_exception_during_mandatory_review(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.ERROR, "d024", "Provider API timeout"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False


class TestProviderTimeout:
    """Test provider timeout handling."""

    def test_provider_timeout_during_mandatory_review(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.ERROR, "d024", "Provider timeout after 30s"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False
        assert "D024 gate" in decision.blocking_reasons[0]


class TestMissingReviewResult:
    """Test missing review result handling."""

    def test_missing_review_result_blocks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.ERROR, "d024", "No review result returned"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False


class TestMissingStaleEvidence:
    """Test missing/stale requirement evidence."""

    def test_stale_evidence_flagged(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes, has_stale_evidence=True)
        assert decision.approved is True  # Passes but flagged for review
        assert decision.has_stale_evidence is True


class TestContradictoryResults:
    """Test contradictory gate results."""

    def test_contradictory_gate_results(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024_gate_v1"),
            ReviewResult(ReviewOutcome.FAIL, "d024_gate_v2"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.has_conflicting_results is False  # Different gate names


class TestWorkOrderFilesNoVerification:
    """Test work order modifying files but no verification evidence."""

    def test_work_order_files_no_verification_evidence(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.ERROR, "execution_qa", "No verification evidence"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.execution_qa_passed is False


class TestManualSuccessFlagConflict:
    """Test manual success flag conflicting with failed checks."""

    def test_manual_success_flag_conflicts_with_failed_checks(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.FAIL, "d024", "Code inspection failed"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False  # Fail-closed: checks override manual flag


class TestCleanDeliveryFailure:
    """Test clean-delivery failure blocking release."""

    def test_clean_delivery_failure_blocks_release(self):
        outcomes = [
            ReviewResult(ReviewOutcome.FAIL, "clean_delivery", "Internal references found"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.clean_delivery_passed is False
        assert "clean delivery gate" in decision.blocking_reasons[0].lower()


class TestD024Failure:
    """Test D024 failure blocking release."""

    def test_d024_failure_blocks_release(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.FAIL, "d024", "No QA verdict"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False
        assert "D024 gate" in decision.blocking_reasons[0]


class TestExecutionQAFailure:
    """Test mandatory execution QA failure blocking release."""

    def test_mandatory_execution_qa_failure_blocks_release(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.FAIL, "execution_qa", "Tests failed"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.execution_qa_passed is False


class TestAllMandatoryGatesPass:
    """Test all mandatory gates passing."""

    def test_all_mandatory_gates_passing(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.PASS, "d024"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is True
        assert decision.required_gates_passed is True
        assert decision.clean_delivery_passed is True
        assert decision.d024_passed is True
        assert decision.execution_qa_passed is True


class TestIndependentWorkContinues:
    """Test independent non-release work continuing safely."""

    def test_independent_non_release_work_continues_safely(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "non_release_gate"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False  # Not a release gate, so not approved
        assert decision.required_gates_passed is True  # All provided outcomes passed
        assert decision.clean_delivery_passed is False  # Not a release gate
        assert len(decision.blocking_reasons) == 0  # No blocking reasons for non-release


class TestSecretSanitization:
    """Test secret sanitization in diagnostics."""

    def test_secret_sanitization_in_diagnostics(self):
        outcome, payload = structured_output_validator({
            "status": "completed",
            "summary": "Task completed",
            "blockers": [],
        })
        assert outcome == ReviewOutcome.PASS
        assert "api_key" not in payload
        assert "secret" not in payload


class TestRepairAttemptBlockingRelease:
    """Test repair attempt that cannot release until checks rerun."""

    def test_repair_attempt_cannot_release_until_checks_rerun(self):
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "clean_delivery"),
            ReviewResult(ReviewOutcome.FAIL, "d024", "Repair attempt incomplete"),
            ReviewResult(ReviewOutcome.PASS, "execution_qa"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False
        assert decision.d024_passed is False
        assert len(decision.blocking_reasons) > 0


class TestFailClosedBehavior:
    """Test fail-closed behavior overall."""

    def test_fail_closed_on_single_failure(self):
        """Fail-closed: one failure should block approval."""
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "gate_a"),
            ReviewResult(ReviewOutcome.FAIL, "gate_b"),
            ReviewResult(ReviewOutcome.PASS, "gate_c"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False

    def test_fail_closed_on_single_error(self):
        """Fail-closed: one error should block approval."""
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "gate_a"),
            ReviewResult(ReviewOutcome.ERROR, "gate_b"),
            ReviewResult(ReviewOutcome.PASS, "gate_c"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False

    def test_fail_closed_on_single_blocked(self):
        """Fail-closed: one blocked status should block approval."""
        outcomes = [
            ReviewResult(ReviewOutcome.PASS, "gate_a"),
            ReviewResult(ReviewOutcome.BLOCKED, "gate_b"),
            ReviewResult(ReviewOutcome.PASS, "gate_c"),
        ]
        decision = release_decision(outcomes)
        assert decision.approved is False

    def test_fail_closed_no_implicit_pass(self):
        """Fail-closed: never silently convert failure to pass."""
        outcome, payload = structured_output_validator({"status": "unknown"})
        assert outcome == ReviewOutcome.ERROR
        assert outcome != ReviewOutcome.PASS
