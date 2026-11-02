"""Unit tests for Fixed Feature Evals Suite (Item C.7)."""

from pathlib import Path
import pytest

from validators.harness.evals import (
    AuthFeatureScenario,
    ContractGovernanceScenario,
    FixedFeatureEvalRunner,
    ScaffoldFeatureScenario,
)


def test_scaffold_feature_scenario(tmp_path: Path):
    """ScaffoldFeatureScenario verifies repository setup and authoring gate."""
    scenario = ScaffoldFeatureScenario()
    res = scenario.run(tmp_path)
    assert res.passed is True, f"Failures: {res.failures}"
    assert res.scenario_id == "feat_scaffold"
    assert res.metrics.get("authoring_gate_passed") is True


def test_auth_feature_scenario(tmp_path: Path):
    """AuthFeatureScenario verifies deliverable within scope, QA verdict, and trace logging."""
    scenario = AuthFeatureScenario()
    res = scenario.run(tmp_path)
    assert res.passed is True, f"Failures: {res.failures}"
    assert res.scenario_id == "feat_auth"
    assert (tmp_path / "src" / "auth.py").is_file()
    assert (tmp_path / ".sync" / "reviews" / "gemma_WO-010_verdict.md").is_file()
    assert res.metrics.get("trace_total_cost", 0.0) > 0


def test_contract_governance_scenario(tmp_path: Path):
    """ContractGovernanceScenario verifies CCR submission, approval, and scope update."""
    scenario = ContractGovernanceScenario()
    res = scenario.run(tmp_path)
    assert res.passed is True, f"Failures: {res.failures}"
    assert res.scenario_id == "feat_contract_change"
    assert res.metrics.get("new_contract_revision") == 2
    assert "CCR-" in res.metrics.get("ccr_id", "")


def test_fixed_feature_eval_runner_full_suite(tmp_path: Path):
    """FixedFeatureEvalRunner runs all canonical scenarios with 100% success rate."""
    runner = FixedFeatureEvalRunner()
    summary = runner.run_all(tmp_path)

    assert summary.total_scenarios == 3
    assert summary.passed_scenarios == 3
    assert summary.failed_scenarios == 0
    assert summary.success_rate == 100.0
    assert len(summary.results) == 3

    scenario_ids = [r["scenario_id"] for r in summary.results]
    assert scenario_ids == ["feat_scaffold", "feat_auth", "feat_contract_change"]
