"""Fail-closed review and orchestration for harness gate evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable


class ReviewOutcome(Enum):
    """Explicit outcomes for structured review validation."""
    PASS = auto()
    FAIL = auto()
    BLOCKED = auto()
    ERROR = auto()


@dataclass(frozen=True)
class ReviewResult:
    """Structured result of one gate review."""
    outcome: ReviewOutcome
    gate_name: str
    details: str | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReleaseDecision:
    """Overall release decision derived from all gates."""
    approved: bool
    required_gates_passed: bool
    clean_delivery_passed: bool
    d024_passed: bool
    execution_qa_passed: bool
    all_outcomes: tuple[ReviewResult, ...]
    blocking_reasons: tuple[str, ...]
    has_stale_evidence: bool = False
    has_conflicting_results: bool = False


def structured_output_validator(
    payload: dict[str, Any],
    *,
    max_retries: int = 2,
    refetch_fn: Callable[[], dict[str, Any]] | None = None,
) -> tuple[ReviewOutcome, dict[str, Any]]:
    """
    Validate structured JSON output with bounded retry and fail-closed semantics.

    Returns (outcome, validated_payload). If outcome is not PASS, the payload
    must NOT be used to make release decisions.
    """
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        try:
            # Basic schema validation checks
            if not isinstance(payload, dict):
                raise ValueError("payload must be a dictionary")

            status = payload.get("status", "")
            if not isinstance(status, str):
                raise ValueError("status must be a string")

            status_lower = status.lower().strip()
            if status_lower not in ("completed", "blocked", "success", "failed", "error"):
                raise ValueError(f"invalid status: {status}")

            # For completed status, ensure required fields
            if status_lower == "completed":
                blockers = payload.get("blockers", [])
                if isinstance(blockers, list) and blockers:
                    raise ValueError("completed status should have empty blockers")

            return ReviewOutcome.PASS, payload

        except (ValueError, TypeError, KeyError) as exc:
            last_error = str(exc)
            if attempt < max_retries and refetch_fn is not None:
                try:
                    payload = refetch_fn()
                    continue
                except Exception:
                    pass
            break

    # Fail closed: return ERROR with last error message
    return ReviewOutcome.ERROR, {"_validation_error": last_error or "invalid structured output"}


def release_decision(
    outcomes: list[ReviewResult],
    *,
    has_stale_evidence: bool = False,
    conflicting: bool = False,
) -> ReleaseDecision:
    """
    Derive overall release decision from individual gate outcomes.

    Fail-closed semantics: ANY failure, block, or error prevents approval.
    """
    if not outcomes:
        return ReleaseDecision(
            approved=False,
            required_gates_passed=False,
            clean_delivery_passed=False,
            d024_passed=False,
            execution_qa_passed=False,
            all_outcomes=(),
            blocking_reasons=("no gate outcomes provided",),
            has_stale_evidence=has_stale_evidence,
            has_conflicting_results=conflicting,
        )

    blocking_reasons: list[str] = []
    clean_delivery_passed = False
    d024_passed = False
    execution_qa_passed = False

    for result in outcomes:
        gate = result.gate_name.lower()
        if "clean_delivery" in gate or "clean-delivery" in gate:
            if result.outcome == ReviewOutcome.PASS:
                clean_delivery_passed = True
            else:
                blocking_reasons.append(
                    f"clean delivery gate: {result.outcome.name}"
                    f"{': {result.details}' if result.details else ''}"
                )
        elif "d024" in gate:
            if result.outcome == ReviewOutcome.PASS:
                d024_passed = True
            else:
                blocking_reasons.append(
                    f"D024 gate: {result.outcome.name}"
                    f"{': {result.details}' if result.details else ''}"
                )
        elif "execution_qa" in gate or "execution-qa" in gate:
            if result.outcome == ReviewOutcome.PASS:
                execution_qa_passed = True
            else:
                blocking_reasons.append(
                    f"execution QA gate: {result.outcome.name}"
                    f"{': {result.details}' if result.details else ''}"
                )

    # All outcomes must PASS for approval
    all_passed = all(o.outcome == ReviewOutcome.PASS for o in outcomes)
    approved = all_passed and clean_delivery_passed and d024_passed and execution_qa_passed

    return ReleaseDecision(
        approved=approved,
        required_gates_passed=all_passed,
        clean_delivery_passed=clean_delivery_passed,
        d024_passed=d024_passed,
        execution_qa_passed=execution_qa_passed,
        all_outcomes=tuple(outcomes),
        blocking_reasons=tuple(blocking_reasons),
        has_stale_evidence=has_stale_evidence,
        has_conflicting_results=conflicting,
    )


def evaluate_release_gates(
    clean_delivery_passed: bool,
    d024_passed: bool,
    requirements_passed: bool,
    execution_qa_passed: bool,
    has_stale_evidence: bool = False,
    has_requirements_blocked: bool = False,
    requirements_blockers: tuple[str, ...] = (),
    execution_qa_blocked: bool = False,
    execution_blockers: tuple[str, ...] = (),
) -> tuple[bool, tuple[str, ...]]:
    """
    Evaluate all mandatory gates for a release decision.

    This is the authoritative release decision function used by production code.
    It integrates CleanDeliveryGate, D024Gate, requirements coverage, and execution QA.

    Args:
        clean_delivery_passed: Result from CleanDeliveryGate.validate()
        d024_passed: Result from D024Gate.evaluate_work_order()
        requirements_passed: Result from validate_requirements_coverage()
        execution_qa_passed: Whether required execution checks passed
        has_stale_evidence: Whether evidence freshness is stale
        has_requirements_blocked: Whether requirements coverage found blockers
        requirements_blockers: List of requirement coverage issues
        execution_qa_blocked: Whether execution QA found failures
        execution_blockers: List of execution QA issues

    Returns:
        (approved, blocking_reasons) tuple
    """
    reasons: list[str] = []

    # Check CleanDeliveryGate
    if not clean_delivery_passed:
        reasons.append("Clean delivery gate failed: project contains prohibited internal references")

    # Check D024 QA Gate
    if not d024_passed:
        reasons.append("D024 QA gate failed: QA verdict missing or deliverable issues detected")

    # Check Requirements Coverage
    if has_requirements_blocked or not requirements_passed:
        for blocker in requirements_blockers:
            reasons.append(f"Requirements coverage: {blocker}")

    # Check Execution QA
    if execution_qa_blocked or not execution_qa_passed:
        for blocker in execution_blockers:
            reasons.append(f"Execution QA: {blocker}")

    # Check evidence freshness
    if has_stale_evidence:
        reasons.append("Evidence freshness: verification evidence is stale and must be refreshed")

    approved = len(reasons) == 0
    return approved, tuple(reasons)
