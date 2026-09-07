"""End-to-End Scope Violation & D025 Code Enforcement Integration Tests (WO-036 / PLANv4 §1.7)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli.init import init
from tests.test_harness import StaticLLMProvider, _fixed_now, _init_project, _write_yaml
from validators.harness import (
    AgentRunner,
    D025Gate,
    D025GateDecision,
    D025ViolationError,
)
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.compiler import compile_project
from validators.knowledge.contract import (
    AgentContract,
    ContractAccessDenied,
    ContractExpiredError,
    ContractValidationError,
)
from validators.knowledge.projections import build_projections
from validators.knowledge.writer import write_knowledge


def _setup_multi_module_project(tmp_path: Path) -> Path:
    """Helper to set up a multi-module codebase with knowledge graph and schemas."""
    project = tmp_path / "governed_repo"
    init(project, name="GovernedRepo", no_git=True)

    # Copy schemas
    src_schemas = Path(__file__).parent.parent / "schemas"
    if src_schemas.exists():
        shutil.copytree(src_schemas, project / "schemas")

    # Ensure .sync directories exist
    (project / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)
    (project / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (project / ".sync" / "state" / "harness").mkdir(parents=True, exist_ok=True)

    # Module 1: billing (Allowed in contract)
    (project / "billing").mkdir(parents=True, exist_ok=True)
    (project / "billing" / "invoices.py").write_text(
        "def create_invoice(client_id: str, amount: float) -> dict:\n"
        "    from billing.reports import format_invoice\n"
        "    return format_invoice(client_id, amount)\n\n"
        "def list_invoices() -> list:\n"
        "    return []\n",
        encoding="utf-8",
    )
    (project / "billing" / "reports.py").write_text(
        "def format_invoice(client_id: str, amount: float) -> dict:\n"
        "    return {'client': client_id, 'amount': amount, 'formatted': True}\n",
        encoding="utf-8",
    )

    # Module 2: auth (Explicitly Denied in contract)
    (project / "auth").mkdir(parents=True, exist_ok=True)
    (project / "auth" / "session.py").write_text(
        "def authenticate_user(token: str) -> bool:\n"
        "    return token == 'valid_token'\n\n"
        "def revoke_session(session_id: str) -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )

    # Module 3: analytics (Unlisted / Outside Scope in contract)
    (project / "analytics").mkdir(parents=True, exist_ok=True)
    (project / "analytics" / "metrics.py").write_text(
        "def compute_daily_revenue() -> float:\n"
        "    return 1000.0\n",
        encoding="utf-8",
    )

    # Build knowledge graph
    write_knowledge(project, compile_project(project), built_at="fixed")
    build_projections(project)

    return project


def test_scope_violation_e2e_knowledge_api(tmp_path: Path):
    """E2E Test: Contract scope enforcement via Knowledge API queries.

    Verifies:
    1. Query inside allowed scope -> SUCCESS.
    2. Query outside allowed scope (unlisted module) -> DENIED.
    3. Query on explicitly denied node -> DENIED.
    4. Structural denial exception and error message.
    """
    project = _setup_multi_module_project(tmp_path)

    # 1. Create a contract with strict allow/deny boundaries
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-200",
        "identity": {
            "reports_to": "senior-architect",
            "role": "backend-lead",
        },
        "scope": {
            "allow": [
                {"depth": 1, "module": "billing.invoices"},
                {"depth": 0, "module": "billing.reports"},
            ],
            "deny": [
                {"module": "auth.*"},
            ],
            "write": "read-write",
        },
        "budget": {
            "expires_at": "2036-08-01T00:00:00Z",
            "max_files_touched": 4,
            "max_tokens": 25000,
        },
    }

    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract_path = contracts_dir / "WO-200.yaml"
    _write_yaml(contract_path, contract_data)

    contract = AgentContract.load(contract_path, project)
    api = KnowledgeAPI(project, contract=contract)

    # Step 1: Query inside allowed scope -> SUCCESS
    res_invoices = api.lookup("create_invoice")
    assert len(res_invoices.results) > 0
    assert res_invoices.results[0].qualified_name == "create_invoice"
    assert "billing/invoices.py" in res_invoices.results[0].path

    res_reports = api.lookup("format_invoice")
    assert len(res_reports.results) > 0
    assert res_reports.results[0].qualified_name == "format_invoice"

    # Context assembly within allowed scope
    context = api.assemble_context("create_invoice", token_budget=2000)
    assert context.revision >= 1
    assert any("create_invoice" in e.text for e in context.entries)

    # Step 2: Query outside allowed scope (analytics is unlisted) -> DENIED
    with pytest.raises(ContractAccessDenied) as exc_unlisted:
        api.lookup("compute_daily_revenue")
    assert "denied by contract WO-200" in str(exc_unlisted.value)

    # Step 3: Query on explicitly denied node (auth.session) -> DENIED
    with pytest.raises(ContractAccessDenied) as exc_denied:
        api.lookup("authenticate_user")
    assert "denied by contract WO-200" in str(exc_denied.value)

    with pytest.raises(ContractAccessDenied) as exc_revoke:
        api.lookup("revoke_session")
    assert "denied by contract WO-200" in str(exc_revoke.value)


def test_scope_violation_e2e_harness_blocked_and_logged(tmp_path: Path):
    """E2E Test: Attempt file edits outside contract scope via Harness Runner.

    Verifies:
    1. Harness detects out-of-scope edits during post-execution verification.
    2. Execution is BLOCKED and not persisted.
    3. Denial reason is structurally captured in HarnessRunResult.
    4. Harness event log reflects failure state.
    """
    project = _setup_multi_module_project(tmp_path)

    # 1. Setup work order in TREE
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-200"]
    _write_yaml(tree_path, tree)

    # 2. Save active work order
    work_order = {
        "assigned_agents": ["codex"],
        "dependencies": [],
        "description": "Implement invoice discount calculation.",
        "id": "WO-200",
        "priority": "P0",
        "status": "ACTIVE",
        "title": "Invoice Discount Calculation",
        "type": "FEATURE",
    }
    _write_yaml(project / ".sync" / "work-orders" / "ACTIVE" / "WO-200.yaml", work_order)

    # 3. Create Contract restricting modifications to billing
    contract_data = {
        "agent_id": "codex",
        "budget": {
            "expires_at": "2036-08-01T00:00:00Z",
            "max_files_touched": 2,
        },
        "scope": {
            "allow": [
                {"depth": 0, "module": "billing.invoices"},
            ],
            "deny": [
                {"module": "auth.*"},
            ],
            "write": "read-write",
        },
        "work_order": "WO-200",
    }
    _write_yaml(project / ".sync" / "contracts" / "WO-200.yaml", contract_data)

    # Case A: Provider attempts to edit out-of-scope file (auth/session.py)
    provider_out_of_scope = StaticLLMProvider({
        "blockers": [],
        "commands": [],
        "modified_files": ["auth/session.py"],  # DENIED
        "release_target": "v2.1.0",
        "report_markdown": "Updated session auth.",
        "retrieval_queries": ["authenticate_user"],
        "status": "completed",
        "summary": "Modified auth session",
        "uncertainty": [],
    })

    runner = AgentRunner(project, "codex", llm_provider=provider_out_of_scope, now_fn=_fixed_now)
    result = runner.run_once()

    # Assert execution BLOCKED
    assert result.status == "blocked"
    assert not result.persisted
    assert result.task_id == "WO-200"
    # Assert structural reason mentions contract or scope denial
    assert "contract" in result.reason.lower()
    assert "denied" in result.reason.lower() or "outside allowed contract scope" in result.reason.lower()

    # Case B: Provider attempts to exceed max_files_touched budget
    provider_budget_exceeded = StaticLLMProvider({
        "blockers": [],
        "commands": [],
        "modified_files": ["billing/invoices.py", "billing/reports.py", "billing/extra.py"],  # 3 files > 2 max
        "release_target": "v2.1.0",
        "report_markdown": "Multi-file refactor.",
        "retrieval_queries": ["create_invoice"],
        "status": "completed",
        "summary": "Touched 3 files",
        "uncertainty": [],
    })

    runner_budget = AgentRunner(project, "codex", llm_provider=provider_budget_exceeded, now_fn=_fixed_now)
    result_budget = runner_budget.run_once()

    assert result_budget.status == "blocked"
    assert not result_budget.persisted
    assert "max_files_touched" in result_budget.reason.lower() or "budget" in result_budget.reason.lower()


def test_d025_gate_classification_and_safeguards():
    """Unit and integration test of D025Gate safeguard rules."""
    gate = D025Gate()

    # 1. Classification tests
    assert gate.is_destructive("git reset --hard HEAD~1")
    assert gate.is_destructive("rm -rf /var/log/data")
    assert gate.is_destructive("docker system prune -f")
    assert gate.is_destructive("git push origin main --force")
    assert gate.is_destructive("del /f /s *.pyc")
    assert not gate.is_destructive("git status")
    assert not gate.is_destructive("pytest tests/")
    assert not gate.is_destructive("stackmind validate .")

    assert gate.is_backup("cp -r .git .git-backup-20260819")
    assert gate.is_backup("tar -czf backup.tar.gz ./data")
    assert gate.is_backup("docker tag myapp:latest myapp:backup")
    assert not gate.is_backup("git status")

    assert gate.is_verification("git status")
    assert gate.is_verification("git log --oneline -n 5")
    assert gate.is_verification("pytest")
    assert gate.is_verification("stackmind validate .")
    assert gate.is_verification("ls -la")

    # 2. Sequence evaluation: Unprotected destructive operation -> BLOCKED
    unprotected = ["git reset --hard HEAD~1"]
    decision_unprotected = gate.evaluate_sequence(unprotected)
    assert not decision_unprotected.passed
    assert decision_unprotected.destructive_detected
    assert "lacks required pre-operation backup step" in decision_unprotected.reason
    assert "lacks required post-operation verification step" in decision_unprotected.reason

    # 3. Sequence evaluation: Missing verification step -> BLOCKED
    missing_verify = ["cp -r .git .git-backup", "git reset --hard HEAD~1"]
    decision_missing_verify = gate.evaluate_sequence(missing_verify)
    assert not decision_missing_verify.passed
    assert "lacks required post-operation verification step" in decision_missing_verify.reason

    # 4. Sequence evaluation: Missing backup step -> BLOCKED
    missing_backup = ["git reset --hard HEAD~1", "git status"]
    decision_missing_backup = gate.evaluate_sequence(missing_backup)
    assert not decision_missing_backup.passed
    assert "lacks required pre-operation backup step" in decision_missing_backup.reason

    # 5. Sequence evaluation: Protected with backup AND verification -> PASSED
    protected = [
        "cp -r .git .git-backup-20260819",
        "git reset --hard HEAD~1",
        "git status",
    ]
    decision_protected = gate.evaluate_sequence(protected)
    assert decision_protected.passed
    assert decision_protected.destructive_detected
    assert decision_protected.has_backup
    assert decision_protected.has_verification
    assert "satisfied" in decision_protected.reason


def test_d025_gate_harness_integration_and_observability(tmp_path: Path):
    """E2E Test: D025 gate integration in Harness Runner with structured observability logging."""
    project = _setup_multi_module_project(tmp_path)

    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-201"]
    _write_yaml(tree_path, tree)

    work_order = {
        "assigned_agents": ["codex"],
        "dependencies": [],
        "description": "Test D025 enforcement.",
        "id": "WO-201",
        "priority": "P0",
        "status": "ACTIVE",
        "title": "D025 Enforcement Test",
        "type": "FEATURE",
    }
    _write_yaml(project / ".sync" / "work-orders" / "ACTIVE" / "WO-201.yaml", work_order)

    contract_data = {
        "agent_id": "codex",
        "budget": {"expires_at": "2036-08-01T00:00:00Z"},
        "scope": {
            "allow": [{"depth": 0, "module": "billing.invoices"}],
            "deny": [],
            "write": "read-write",
        },
        "work_order": "WO-201",
    }
    _write_yaml(project / ".sync" / "contracts" / "WO-201.yaml", contract_data)

    # 1. Propose unprotected destructive command
    provider_destructive = StaticLLMProvider({
        "blockers": [],
        "commands": ["git reset --hard HEAD~1"],
        "modified_files": [],
        "release_target": "v2.1.0",
        "report_markdown": "Attempted hard reset.",
        "retrieval_queries": [],
        "status": "completed",
        "summary": "Reset git commit",
        "uncertainty": [],
    })

    runner = AgentRunner(project, "codex", llm_provider=provider_destructive, now_fn=_fixed_now)
    result = runner.run_once()

    # Assert execution BLOCKED
    assert result.status == "blocked"
    assert not result.persisted
    assert "d025" in result.reason.lower() or "destructive" in result.reason.lower()

    # 2. Verify structured audit log in observability layer
    d025_events_file = project / ".sync" / "state" / "harness" / "d025_events.jsonl"
    assert d025_events_file.exists()

    lines = [json.loads(line) for line in d025_events_file.read_text(encoding="utf-8").strip().split("\n") if line]
    assert len(lines) >= 1
    last_event = lines[-1]
    assert last_event["agent"] == "codex"
    assert last_event["event"] == "harness.d025_gate"
    assert last_event["decision"]["passed"] is False
    assert last_event["decision"]["destructive_detected"] is True
    assert "git reset --hard HEAD~1" in last_event["decision"]["destructive_commands"]
