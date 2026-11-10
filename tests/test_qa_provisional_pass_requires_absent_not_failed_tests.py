"""A provisional QA pass may only be granted when tests are ABSENT, never when they FAILED."""
from __future__ import annotations

from pathlib import Path

import pytest

from validators.harness import review_gate as rg


def _workspace(tmp_path: Path) -> Path:
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    (wo_dir / "WO-001.yaml").write_text(
        "id: WO-001\ndeliverable:\n  type: code\n  path: calc.js\n", encoding="utf-8"
    )
    # Syntactically valid but functionally wrong: the static verifier cannot catch this.
    (tmp_path / "calc.js").write_text(
        "function calc(x) { return x - 2; }\nmodule.exports = { calc };\n", encoding="utf-8"
    )
    return tmp_path


def _verdict(ws: Path, verdict: str, evidence: list[dict], actions: list[str] | None = None) -> dict:
    return {
        "schema": rg.QA_VERDICT_SCHEMA,
        "work_order_id": "WO-001",
        "verdict": verdict,
        "subject_hash": rg.subject_hash(ws, "WO-001"),
        "criteria": [{"id": "c1", "description": "calc doubles input", "outcome": "PASS"}],
        "evidence": evidence,
        "findings": [],
        "required_actions": actions or [],
    }


@pytest.mark.parametrize(
    "evidence",
    [
        [{"command": "pytest tests/test_calc.py", "passed": False, "result": "3 failed, 2 passed"}],
        [{"command": "run_tests", "passed": False, "result": "AssertionError: calc(2) == 4"}],
        [{"command": "glob tests/**", "passed": False, "result": "Traceback (most recent call last)"}],
    ],
)
def test_real_test_failures_are_never_provisional(tmp_path: Path, evidence: list[dict]) -> None:
    ws = _workspace(tmp_path)
    ok, reason = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE, evidence, ["fix calc to double its input, then re-check"]), ws, "WO-001"
    )
    assert ok is False, reason


@pytest.mark.parametrize(
    "evidence",
    [
        [{"command": "pytest", "passed": False, "exit_code": 5, "result": "no tests ran"}],
        [{"command": "pytest tests/", "passed": False, "result": "ERROR: file or directory not found: tests/"}],
        [{"command": "pytest", "passed": False, "result": "exit code 4"}],
    ],
)
def test_genuinely_absent_tests_still_provisional(tmp_path: Path, evidence: list[dict]) -> None:
    ws = _workspace(tmp_path)
    (ws / "calc.js").write_text("function calc(x) { return x * 2; }\nmodule.exports = { calc };\n", encoding="utf-8")
    ok, reason = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE, evidence, ["Author tests in tests/"]), ws, "WO-001"
    )
    assert ok is True, reason


def test_generic_check_action_no_longer_launders_functional_defects(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    ok, _ = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE,
                 [{"command": "pytest", "passed": False, "exit_code": 5, "result": "no tests ran"}],
                 ["check that calc handles negative numbers"]), ws, "WO-001"
    )
    assert ok is False


def test_delivery_gate_explains_provisional_verdict(tmp_path: Path) -> None:
    import json
    from validators.harness import delivery_integrity as di

    vp = rg.qa_verdict_path(tmp_path, "WO-001")
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text(json.dumps({"verdict": rg.QA_NO_TEST_EVIDENCE}), encoding="utf-8")
    status, reason = di.qa_status(tmp_path, "WO-001")
    assert status == di.QA_INCONCLUSIVE  # still not deliverable
    assert "provisional" in reason and "unrecognized" not in reason


@pytest.mark.parametrize(
    "result",
    [
        "10 tests did not complete: timeout after 60s",
        "Worker crashed while running 100 tests",
        "no build errors, but the test runner exited 1",
        "no such file or directory: calc.js",
    ],
)
def test_substring_lookalikes_are_not_missing_tests(tmp_path: Path, result: str) -> None:
    ws = _workspace(tmp_path)
    ok, reason = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE, [{"command": "pytest", "passed": False, "result": result}],
                 ["Author tests in tests/"]), ws, "WO-001"
    )
    assert ok is False, reason

@pytest.mark.parametrize(
    "result",
    [
        "No build config, bundler, Makefile, or static check defined in project",
        "No build configuration or build script detected in project",
        "No build manifest or build script declared",
        "no test framework configured",
        "no test runner installed",
        "pytest exited 5, no tests collected (0 tests ran) - no automated coverage for README deliverable",
    ],
)
def test_absent_build_and_test_evidence_accepted_for_provisional(tmp_path: Path, result: str) -> None:
    ws = _workspace(tmp_path)
    ok, reason = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE, [{"command": "glob **/package.json", "passed": False, "result": result}],
                 ["Author tests in tests/"]), ws, "WO-001"
    )
    assert ok is True, reason


@pytest.mark.parametrize(
    "result",
    [
        "no test runner responded: connection lost after 30s",
        "no build script errors, but test step exited 1",
        "no test framework detected a crash; process killed (SIGSEGV)",
        "no test config found for the failing suite 'auth'; suite aborted with exit 1",
        "no package.json errors but jest threw TypeError at calc.js:2",
        "pytest exited 1 after running the suite",
    ],
)
def test_absence_phrases_must_end_as_absence_statements(tmp_path: Path, result: str) -> None:
    ws = _workspace(tmp_path)
    ok, reason = rg.validate_qa_verdict(
        _verdict(ws, rg.QA_NO_TEST_EVIDENCE, [{"command": "npm test", "passed": False, "result": result}],
                 ["Author tests in tests/"]), ws, "WO-001"
    )
    assert ok is False, reason
