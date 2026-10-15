"""Tests for contract scope precedence: specific allow rules overriding broad deny rules.

Guarantees that broad denials like `.sync/**` cannot block explicitly authorized
governance channels (.sync/inbox/claude/**, .sync/decisions/**) at any layer:
- ContractEvaluator (kernel/contract.py)
- Supervisor recovery and dependency channel grants (kernel/daemon/supervisor.py)
- Harness post-turn scope verification (harness/runner.py)
- Pre-execution contract boundary gate (harness/contract_gate.py)
"""

from pathlib import Path
import pytest
import yaml

from validators.kernel.contract import AgentContract, ContractEvaluator


def test_contract_evaluator_specific_allow_overrides_broad_deny():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="gemma",
        work_order="WO-004",
        allow=("tests/**", "index.html", ".sync/inbox/claude/**"),
        deny=(".git/**", ".sync/**"),
        write_mode="read-write",
    )

    # 1. Explicitly allowed protocol channel write succeeds despite broad .sync/** deny
    ok, msg = evaluator.authorize(contract, "write_file", ".sync/inbox/claude/WO-004-qa-verdict.md")
    assert ok is True
    assert msg == "authorized"

    # 2. General workspace deliverable in allow succeeds
    ok, msg = evaluator.authorize(contract, "write_file", "index.html")
    assert ok is True
    assert msg == "authorized"

    # 3. Un-allowed file under .sync is denied
    ok, msg = evaluator.authorize(contract, "write_file", ".sync/contracts/WO-001.yaml")
    assert ok is False
    assert "denied" in msg or "outside allowed scope" in msg

    # 4. Git file is denied
    ok, msg = evaluator.authorize(contract, "write_file", ".git/config")
    assert ok is False
    assert "denied" in msg


def test_contract_evaluator_specific_deny_overrides_broad_allow():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="codex",
        work_order="WO-002",
        allow=("src/**",),
        deny=(".git/**", "src/secret/**"),
        write_mode="read-write",
    )

    # 1. Normal file in src/** is allowed
    ok, msg = evaluator.authorize(contract, "write_file", "src/main.py")
    assert ok is True
    assert msg == "authorized"

    # 2. Specific deny inside src/** is denied
    ok, msg = evaluator.authorize(contract, "write_file", "src/secret/key.txt")
    assert ok is False
    assert "target is explicitly denied" in msg


def test_contract_evaluator_decision_recovery_channel():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="claude",
        work_order="WO-004",
        allow=("tests/**", ".sync/decisions/**"),
        deny=(".git/**", ".sync/**"),
        write_mode="read-write",
    )

    ok, msg = evaluator.authorize(contract, "write_file", ".sync/decisions/recovery/WO-004.decision.json")
    assert ok is True
    assert msg == "authorized"


def test_supervisor_ensure_recovery_decision_channel_cleans_deny(tmp_path: Path):
    from unittest.mock import MagicMock
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    ws = tmp_path / "ws"
    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True)
    c_path = contracts_dir / "WO-004.yaml"
    c_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "agent_id": "gemma",
        "work_order": "WO-004",
        "scope": {
            "allow": [{"module": "tests/**"}, {"module": "index.html"}],
            "deny": [{"module": ".git/**"}, {"module": ".sync/**"}],
            "write": "read-write",
        },
    }), encoding="utf-8")

    supervisor = LifecycleSupervisor(MagicMock())
    supervisor._ensure_recovery_decision_channel(ws, "WO-004")

    saved = yaml.safe_load(c_path.read_text(encoding="utf-8"))
    allow_modules = [r.get("module") for r in saved["scope"]["allow"]]
    deny_modules = [r.get("module") for r in saved["scope"]["deny"]]

    assert ".sync/decisions/**" in allow_modules
    assert ".sync/**" not in deny_modules
    assert ".git/**" in deny_modules


def test_supervisor_ensure_dependency_escalation_channel_cleans_deny(tmp_path: Path):
    from unittest.mock import MagicMock
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    ws = tmp_path / "ws"
    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True)
    c_path = contracts_dir / "WO-002.yaml"
    c_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-002",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [{"module": ".git/**"}, {"module": ".sync/**"}, {"module": "requirements.txt"}],
            "write": "read-write",
        },
    }), encoding="utf-8")

    supervisor = LifecycleSupervisor(MagicMock())
    supervisor._ensure_dependency_escalation_channel(ws, "WO-002")

    saved = yaml.safe_load(c_path.read_text(encoding="utf-8"))
    allow_modules = [r.get("module") for r in saved["scope"]["allow"]]
    deny_modules = [r.get("module") for r in saved["scope"]["deny"]]

    assert "requirements.txt" in allow_modules
    assert ".sync/inbox/**" in allow_modules
    assert ".sync/**" not in deny_modules
    assert "requirements.txt" not in deny_modules
    assert ".git/**" in deny_modules


def test_architecture_agent_read_only_workspace_research():
    """Verify Lead Architect (claude) is authorized for read-only codebase research across workspace."""
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="claude",
        work_order="WO-000",
        allow=("PLAN.md", ".sync/work-orders/**", ".sync/contracts/**"),
        deny=(".git/**", "src/**", ".env"),
        write_mode="read-write",
    )

    # 1. Reading assets and root documents not explicitly in allow is authorized for architecture
    ok, msg = evaluator.authorize(contract, "read_file", "genset_full.svg")
    assert ok is True
    assert msg == "authorized"

    ok, msg = evaluator.authorize(contract, "read_file", "index.html")
    assert ok is True
    assert msg == "authorized"

    # 2. Reading and listing src/** is authorized for architecture research even if in old deny
    ok, msg = evaluator.authorize(contract, "read_file", "src/backend.py")
    assert ok is True
    assert msg == "authorized"

    ok, msg = evaluator.authorize(contract, "list_directory", "src")
    assert ok is True
    assert msg == "authorized"

    # 3. Writing to src/backend.py or index.html is strictly DENIED (architects plan; workers code)
    ok, msg = evaluator.authorize(contract, "write_file", "src/backend.py")
    assert ok is False
    assert "outside allowed scope" in msg or "denied" in msg

    ok, msg = evaluator.authorize(contract, "write_file", "index.html")
    assert ok is False
    assert "outside allowed scope" in msg or "denied" in msg

    # 4. Writing to allowed deliverables (PLAN.md) succeeds
    ok, msg = evaluator.authorize(contract, "write_file", "PLAN.md")
    assert ok is True
    assert msg == "authorized"

    # 5. Reading security boundaries (.git, .env, other agent inboxes) is strictly DENIED
    ok, msg = evaluator.authorize(contract, "read_file", ".git/config")
    assert ok is False
    assert "explicitly denied" in msg

    ok, msg = evaluator.authorize(contract, "read_file", ".env")
    assert ok is False
    assert "explicitly denied" in msg

    ok, msg = evaluator.authorize(contract, "read_file", ".sync/inbox/codex/secret.md")
    assert ok is False
    assert "explicitly denied" in msg

    # 6. Reading Claude's own inbox is allowed
    ok, msg = evaluator.authorize(contract, "read_file", ".sync/inbox/claude/message.md")
    assert ok is True
    assert msg == "authorized"

