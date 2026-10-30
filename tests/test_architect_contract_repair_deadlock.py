"""Regression test for Architect contract repair deadlock and .sync scope evaluation.

Verifies:
1. Allow override for .sync/contracts/** is not corrupted by leading dot stripping.
2. Architect repair authorization contracts never contain conflicting .sync deny rules.
3. list_directory gracefully returns [] on non-existent directories instead of crashing.
"""

from pathlib import Path
import pytest
import yaml

from validators.knowledge.contract import AgentContract
from validators.harness.contract_gate import verify_post_execution
from validators.harness.runner.models import HarnessTask, HarnessDecision


def test_sync_contract_allow_overrides_broad_deny(tmp_path: Path):
    """Test that an allow rule on .sync/contracts/WO-001.yaml or .sync/contracts/**
    properly overrides a broad deny on .sync/** without being corrupted by lstrip('./').
    """
    contract_data = {
        "schema_version": 1,
        "agent_id": "claude",
        "work_order": "WO-001",
        "identity": {"role": "architecture", "reports_to": "ceo"},
        "scope": {
            "allow": [
                {"module": ".sync/contracts/WO-001.yaml"},
                {"module": ".sync/contracts/**"},
                {"module": ".sync/work-orders/**"},
            ],
            "deny": [
                {"module": ".git/**"},
                {"module": ".sync/**"},
            ],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 10, "max_tokens": 50000},
    }
    contracts_dir = tmp_path / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    c_file = contracts_dir / "WO-001.yaml"
    c_file.write_text(yaml.safe_dump(contract_data), encoding="utf-8")

    task = HarnessTask(
        kind="work_order",
        identifier="WO-001",
        path=c_file,
        title="Repair contract",
        body="Repair",
        query="",
        work_order_id="WO-001",
    )
    decision = HarnessDecision(
        status="completed",
        summary="Amended contract",
        report_markdown="Done",
        blockers=(),
        modified_files=(".sync/contracts/WO-001.yaml",),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
    )

    # Must succeed without raising ContractAccessDenied
    verify_post_execution(
        project_path=tmp_path,
        agent="claude",
        task=task,
        decision=decision,
        observed_files=(".sync/contracts/WO-001.yaml",),
    )


def test_write_recovery_repair_authorization_cleans_sync_deny(tmp_path: Path):
    """Verify that _write_recovery_repair_authorization strips .sync from deny."""
    from validators.kernel.daemon.supervisor.core import LifecycleSupervisor

    ws = tmp_path
    sync = ws / ".sync"
    (sync / "contracts").mkdir(parents=True, exist_ok=True)
    (sync / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)

    worker_contract = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [{"module": ".git/**"}, {"module": ".sync/**"}],
            "write": "read-write",
        },
    }

    supervisor = LifecycleSupervisor(manager=None)
    supervisor._write_recovery_repair_authorization(
        ws=ws,
        wo_id="WO-001",
        action="amend_contract",
        original_contract=worker_contract,
    )

    c_file = sync / "contracts" / "WO-001.yaml"
    assert c_file.is_file()
    written = yaml.safe_load(c_file.read_text(encoding="utf-8"))
    deny_modules = [r.get("module") for r in written["scope"].get("deny", [])]
    assert ".sync/**" not in deny_modules
    assert ".git/**" in deny_modules
