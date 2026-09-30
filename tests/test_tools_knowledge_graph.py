"""Tests for Phase 4 Knowledge Graph Intelligence tools in ToolGateway."""

from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "claude", allow_patterns=None, deny_patterns=None):
    if allow_patterns is None:
        allow_patterns = ["**"]
    if deny_patterns is None:
        deny_patterns = []

    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": allow_patterns,
            "denied": deny_patterns,
            "write": "read-write",
        },
    })
    session = AgentSession(agent, "test-provider", str(tmp_path), session_id="s1")
    attempt = session.create_attempt(contract, attempt_id="a1")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = get_role_policy(agent)
    workspace = ScratchWorkspace(authoritative_root=tmp_path, root=tmp_path, attempt_id="a1")
    gateway = ToolGateway(
        boundary=boundary,
        policy=policy,
        contract=attempt.contract,
        workspace=workspace,
        sandbox=None,
        session_id="s1",
        attempt_id="a1",
        actor_id=agent,
        provider_id="test-provider",
    )
    return gateway, journal


def test_callers_and_callees(tmp_path):
    code = (
        "def low_level_calc(x):\n"
        "    return x * 2\n"
        "\n"
        "def middle_service(x):\n"
        "    val = low_level_calc(x)\n"
        "    return val + 1\n"
        "\n"
        "def entrypoint():\n"
        "    return middle_service(5)\n"
    )
    (tmp_path / "calc.py").write_text(code, encoding="utf-8")

    gateway, journal = _setup_gateway(tmp_path, "claude")

    # Find callers of low_level_calc
    callers = gateway.find_callers("low_level_calc")
    assert "middle_service" in callers

    # Find callees of middle_service
    callees = gateway.find_callees("middle_service")
    assert "low_level_calc" in callees

    assert any(r.request.operation_type == OperationType.FIND_CALLERS for r in journal.records)
    assert any(r.request.operation_type == OperationType.FIND_CALLEES for r in journal.records)


def test_impact_analysis(tmp_path):
    code = (
        "def compute():\n"
        "    return 42\n"
        "\n"
        "def caller_one():\n"
        "    return compute()\n"
        "\n"
        "def caller_two():\n"
        "    return compute() + 1\n"
    )
    (tmp_path / "impact_mod.py").write_text(code, encoding="utf-8")

    gateway, journal = _setup_gateway(tmp_path, "codex")

    impact = gateway.impact_analysis("compute")
    assert impact["symbol"] == "compute"
    assert "caller_one" in impact["impacted_symbols"]
    assert "caller_two" in impact["impacted_symbols"]
    assert "impact_mod.py" in impact["impacted_files"]
    assert impact["impact_score"] > 0


def test_dependency_analysis(tmp_path):
    mod_a = tmp_path / "moda.py"
    mod_a.write_text("import sys\nimport os\n", encoding="utf-8")

    mod_b = tmp_path / "modb.py"
    mod_b.write_text("import moda\n", encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "claude")

    dep_a = gateway.dependency_analysis("moda.py")
    assert "sys" in dep_a["imports"]
    assert "os" in dep_a["imports"]
    assert "modb" in dep_a["imported_by"]


def test_data_flow_analysis_and_stats(tmp_path):
    (tmp_path / "simple.py").write_text("x = 1\n", encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "gemini")

    flow = gateway.data_flow_analysis("input_data", "output_data")
    assert flow["source"] == "input_data"
    assert flow["sink"] == "output_data"
    assert len(flow["paths"]) >= 1

    stats = gateway.knowledge_stats()
    assert "revision" in stats
    assert "python_files" in stats or "indexed_symbols" in stats
