"""Tests for Phase E: D024 QA Gate and GitOps enforcement."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from tests.test_harness import StaticLLMProvider, _fixed_now, _init_project, _write_yaml
from validators.harness import (
    AgentRunner,
    D024Gate,
    D024GateDecision,
    D024ViolationError,
)
from validators.kernel.daemon import DaemonStorage, SessionManager


def _setup_gate_scenario(
    tmp_path: Path,
    *,
    wo_target: str = "WO-101",
    wo_gitops: str = "WO-102",
    deliverable_path: str = "src/app.py",
    deliverable_content: str | None = "print('hello')\n",
    verdict_type: str | None = "APPROVED",  # "APPROVED", "NEEDS_CHANGES", "BLOCKED", or None
    verdict_filename: str = "2026-09-27_gemma_WO-101-verdict.md",
    create_test_file: bool = True,
) -> Path:
    project = _init_project(tmp_path)

    # 1. Setup deliverable file
    if deliverable_content is not None:
        p = project / deliverable_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(deliverable_content, encoding="utf-8")

        if create_test_file:
            t = project / "tests" / "test_app.py"
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text("def test_app():\n    assert True\n", encoding="utf-8")

    # 2. Setup implementation work order (WO-101)
    target_wo = {
        "id": wo_target,
        "type": "FEATURE",
        "title": "Backend feature implementation",
        "status": "COMPLETED",
        "priority": "P2",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "path": deliverable_path,
            "description": "Feature deliverable",
        },
        "description": "Feature work order",
    }
    _write_yaml(project / ".sync" / "work-orders" / "COMPLETED" / f"{wo_target}.yaml", target_wo)

    # 3. Setup GitOps work order (WO-102)
    gitops_wo = {
        "id": wo_gitops,
        "type": "FEATURE",
        "title": f"Commit and release {wo_target}",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["local-llm"],
        "dependencies": [wo_target],
        "contract_ref": f".sync/contracts/{wo_gitops}.yaml",
        "deliverable": {
            "type": "code",
            "path": "VERSION.md",
            "description": "Release metadata update",
        },
        "description": f"Perform release hygiene and commit for {wo_target}",
    }
    _write_yaml(project / ".sync" / "work-orders" / "ACTIVE" / f"{wo_gitops}.yaml", gitops_wo)

    # Ensure VERSION.md exists
    (project / "VERSION.md").write_text("# Version\n3.1.0\n", encoding="utf-8")

    # 4. Update INDEX.yaml and TREE.yaml
    index_path = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {"orders": []}
    orders = index_data.get("orders", [])
    orders.append({
        "id": wo_target,
        "title": target_wo["title"],
        "status": "COMPLETED",
        "assigned": "codex",
        "priority": "P2",
        "dependencies": [],
        "type": "FEATURE",
        "deliverable": deliverable_path,
    })
    orders.append({
        "id": wo_gitops,
        "title": gitops_wo["title"],
        "status": "ACTIVE",
        "assigned": "local-llm",
        "priority": "P0",
        "dependencies": [wo_target],
        "type": "FEATURE",
        "deliverable": "VERSION.md",
    })
    index_data["orders"] = orders
    index_data["total_active"] = sum(1 for o in orders if o.get("status") == "ACTIVE")
    index_data["total_completed"] = sum(1 for o in orders if o.get("status") == "COMPLETED")
    _write_yaml(index_path, index_data)

    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["local-llm"]["assigned_work_orders"] = [wo_gitops]
    tree["work_orders"] = {
        "total_active": index_data["total_active"],
        "total_completed": index_data["total_completed"],
        "total_blocked": 0,
    }
    _write_yaml(tree_path, tree)

    # 5. Setup GitOps contract
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    gitops_contract = {
        "agent_id": "local-llm",
        "work_order": wo_gitops,
        "scope": {
            "allow": [{"module": "VERSION", "depth": 0}, {"module": "src", "depth": 2}],
            "deny": [],
            "write": "read-write",
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z",
            "max_files_touched": 10,
        },
    }
    _write_yaml(contracts_dir / f"{wo_gitops}.yaml", gitops_contract)

    # 6. Copy schemas for harness execution
    src_schemas = Path(__file__).parent.parent / "schemas"
    if not (project / "schemas").exists():
        shutil.copytree(src_schemas, project / "schemas")

    # 7. Write QA verdict if requested
    if verdict_type is not None:
        inbox_claude = project / ".sync" / "inbox" / "claude"
        inbox_claude.mkdir(parents=True, exist_ok=True)
        verdict_content = (
            f"# {wo_target} Verdict: {verdict_type}\n\n"
            f"- **From**: Gemma (QA Lead)\n"
            f"- **To**: Claude (Senior Architect)\n"
            f"- **Work Order**: {wo_target}\n"
            f"- **Verdict**: **{verdict_type}**\n\n"
            f"Details: QA audit for {wo_target} evaluated with {verdict_type}.\n"
        )
        (inbox_claude / verdict_filename).write_text(verdict_content, encoding="utf-8")

    return project


def test_d024_gate_passes_when_qa_approved_and_deliverable_exists(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type="APPROVED")
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is True
    assert decision.verdict_status == "APPROVED"
    assert decision.target_work_orders == ("WO-101",)
    assert decision.deliverable_exists is True
    assert len(decision.verdict_files) >= 1

    # verify_gitops_preconditions should not raise
    precond = gate.verify_gitops_preconditions(project, "WO-102")
    assert precond.passed is True


def test_d024_gate_blocks_when_verdict_missing(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type=None)
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "MISSING"
    assert "D024 protocol violation" in decision.reason

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "D024 QA Gate Blocked" in str(exc_info.value)
    assert "No QA verdict found" in str(exc_info.value)


def test_d024_gate_blocks_when_verdict_needs_changes(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type="NEEDS_CHANGES")
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"
    assert "rejected by QA" in decision.reason

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "NEEDS_CHANGES" in str(exc_info.value)


def test_d024_gate_blocks_when_verdict_blocked(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type="BLOCKED")
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "BLOCKED"
    assert "BLOCKED" in decision.reason

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "BLOCKED" in str(exc_info.value)


def test_d024_gate_blocks_when_deliverable_missing(tmp_path):
    project = _setup_gate_scenario(tmp_path, deliverable_content=None, verdict_type="APPROVED")
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "INCOMPLETE"
    assert "does not exist" in decision.reason

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "does not exist" in str(exc_info.value)


def test_d024_gate_blocks_when_deliverable_empty(tmp_path):
    project = _setup_gate_scenario(tmp_path, deliverable_content="", verdict_type="APPROVED")
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "INCOMPLETE"
    assert "empty (0 bytes)" in decision.reason

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "empty (0 bytes)" in str(exc_info.value)


def test_d024_gate_resolves_gitops_dependencies_and_text_references(tmp_path):
    project = _init_project(tmp_path)
    gate = D024Gate()

    # Case 1: Direct dependencies
    wo_data1 = {
        "id": "WO-201",
        "type": "RELEASE",
        "assigned_agents": ["local-llm"],
        "dependencies": ["WO-101", "WO-102"],
    }
    targets1 = gate.resolve_target_work_orders(project, "WO-201", wo_data1)
    assert targets1 == ("WO-101", "WO-102")

    # Case 2: Inferred from title and description
    wo_data2 = {
        "id": "WO-202",
        "type": "RELEASE",
        "assigned_agents": ["local-llm"],
        "title": "Release WO-103",
        "description": "Commit deliverables from WO-104 and WO-105",
        "dependencies": [],
    }
    targets2 = gate.resolve_target_work_orders(project, "WO-202", wo_data2)
    assert targets2 == ("WO-103", "WO-104", "WO-105")

    # Case 3: Non-gitops implementation work order
    wo_data3 = {
        "id": "WO-101",
        "type": "FEATURE",
        "assigned_agents": ["codex"],
        "dependencies": ["WO-099"],
    }
    targets3 = gate.resolve_target_work_orders(project, "WO-101", wo_data3)
    assert targets3 == ("WO-101",)


def test_d024_gate_audit_logging(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type="APPROVED")
    gate = D024Gate()
    decision = gate.evaluate_work_order(project, "WO-102")

    audit_file = project / ".sync" / "reports" / "d024_gate_audit.jsonl"
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1

    last_record = json.loads(lines[-1])
    assert last_record["work_order_id"] == "WO-102"
    assert last_record["passed"] is True
    assert last_record["verdict_status"] == "APPROVED"
    assert "timestamp" in last_record


def test_d024_gate_yaml_and_json_verdicts(tmp_path):
    project = _setup_gate_scenario(tmp_path, verdict_type=None)
    qa_dir = project / ".sync" / "qa" / "verdicts"
    qa_dir.mkdir(parents=True, exist_ok=True)
    gate = D024Gate()

    # Test YAML verdict approval
    yaml_verdict = qa_dir / "WO-101-verdict.yaml"
    yaml_verdict.write_text("work_order: WO-101\nverdict: APPROVED\nqa_lead: gemma\n", encoding="utf-8")
    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is True
    assert decision.verdict_status == "APPROVED"

    # Test JSON verdict rejection
    yaml_verdict.unlink()
    json_verdict = qa_dir / "WO-101-verdict.json"
    json_verdict.write_text(json.dumps({"work_order": "WO-101", "verdict": "NEEDS_CHANGES"}), encoding="utf-8")
    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"


def test_agent_runner_blocks_gitops_when_qa_fails(tmp_path):
    """End-to-end: AgentRunner blocks GitOps when QA has not approved the work order."""
    project = _setup_gate_scenario(tmp_path, verdict_type="NEEDS_CHANGES")

    provider = StaticLLMProvider({
        "status": "completed",
        "summary": "Release committed",
        "report_markdown": "Committed changes.",
        "blockers": [],
        "modified_files": [],
        "release_target": "v1.1.0",
        "retrieval_queries": [],
        "uncertainty": [],
    })

    runner = AgentRunner(project, "local-llm", llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()

    assert result.status == "blocked"
    assert not result.persisted
    assert "D024 QA Gate Blocked" in str(result.reason) or "NEEDS_CHANGES" in str(result.reason)


def test_agent_runner_allows_gitops_when_qa_approved(tmp_path):
    """End-to-end: AgentRunner allows GitOps progression when QA is explicitly APPROVED."""
    project = _setup_gate_scenario(tmp_path, verdict_type="APPROVED")

    provider = StaticLLMProvider({
        "status": "completed",
        "summary": "Release committed",
        "report_markdown": "Committed changes.",
        "blockers": [],
        "modified_files": [],
        "release_target": "v1.1.0",
        "retrieval_queries": [],
        "uncertainty": [],
    })

    runner = AgentRunner(project, "local-llm", llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()

    assert result.status == "completed"
    assert result.persisted is True


def test_daemon_manager_blocks_unapproved_gitops_dispatch(tmp_path):
    """Daemon manager dispatches subagents with D024 gate check."""
    project = _setup_gate_scenario(tmp_path, verdict_type="NEEDS_CHANGES")
    manager = SessionManager(DaemonStorage(project / ".sync" / "state" / "daemon"))

    session = manager.create_session(
        agent="claude",
        provider="mock",
        contract={"scope": {"allow": ["*"]}},
        workspace=str(project),
    )
    _, parent_op = manager.begin_operation(session["session_id"], "plan", {})

    with pytest.raises(D024ViolationError) as exc_info:
        manager.dispatch_subagent(
            session_id=session["session_id"],
            parent_operation_id=parent_op,
            role="gitops",
            work_order_id="WO-102",
        )
    assert "D024 QA Gate Blocked" in str(exc_info.value)


def test_daemon_manager_blocks_unapproved_gitops_turn(tmp_path):
    """Daemon manager start_turn blocks GitOps role when QA gate fails."""
    project = _setup_gate_scenario(tmp_path, verdict_type=None)
    manager = SessionManager(DaemonStorage(project / ".sync" / "state" / "daemon"))

    session = manager.create_session(
        agent="local-llm",
        provider="mock",
        contract={"scope": {"allow": ["*"]}},
        workspace=str(project),
    )

    with pytest.raises(D024ViolationError) as exc_info:
        manager.start_turn(
            session["session_id"],
            "Commit release for WO-101",
            role="gitops",
            work_order_id="WO-102",
        )
    assert "D024 QA Gate Blocked" in str(exc_info.value)


def test_daemon_manager_allows_approved_gitops_dispatch(tmp_path):
    """Daemon manager allows GitOps subagent dispatch when QA is APPROVED."""
    project = _setup_gate_scenario(tmp_path, verdict_type="APPROVED")
    manager = SessionManager(DaemonStorage(project / ".sync" / "state" / "daemon"))

    session = manager.create_session(
        agent="claude",
        provider="mock",
        contract={"scope": {"allow": ["*"]}},
        workspace=str(project),
    )
    _, parent_op = manager.begin_operation(session["session_id"], "plan", {})

    op = manager.dispatch_subagent(
        session_id=session["session_id"],
        parent_operation_id=parent_op,
        role="gitops",
        work_order_id="WO-102",
    )
    assert op is not None
    assert op.get("work_order_id") == "WO-102"
    assert op.get("role") == "gitops"


def test_d024_gate_blocks_code_deliverable_when_test_file_missing(tmp_path):
    """Regression test: code deliverable with no companion test file is blocked even if QA is APPROVED."""
    project = _setup_gate_scenario(tmp_path, verdict_type="APPROVED", create_test_file=False)
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"
    assert "no test file found for deliverable src/app.py" in str(decision.reason)

    with pytest.raises(D024ViolationError) as exc_info:
        gate.verify_gitops_preconditions(project, "WO-102")
    assert "no test file found for deliverable src/app.py" in str(exc_info.value)


def test_d024_gate_blocks_code_deliverable_when_insecure_credentials_present(tmp_path):
    """Regression test: code deliverable containing hardcoded credential comparison is blocked even if QA is APPROVED."""
    auth_content = (
        "from flask import Flask, request, jsonify\n"
        "app = Flask(__name__)\n\n"
        "@app.route('/login', methods=['POST'])\n"
        "def login():\n"
        "    data = request.get_json()\n"
        "    username = data.get('username')\n"
        "    password = data.get('password')\n"
        "    if username == 'admin' and password == 'admin':\n"
        "        return jsonify({'message': 'Login successful'}), 200\n"
        "    return jsonify({'message': 'Invalid credentials'}), 401\n"
    )
    project = _setup_gate_scenario(
        tmp_path,
        deliverable_content=auth_content,
        verdict_type="APPROVED",
        create_test_file=True,
    )
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"
    assert "insecure credential pattern in deliverable" in str(decision.reason)
    assert "password == 'admin'" in str(decision.reason)




def test_d024_gate_blocks_on_deterministic_security_findings(tmp_path):
    """The report-class findings — hardcoded env-fallback secrets and enabled
    debug flags — are caught deterministically by the deliverable scan."""
    vulnerable_content = (
        "import os\n"
        "from flask import Flask\n"
        "\n"
        "SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-12345')\n"
        "DEBUG = True\n"
        "\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.run(debug=True)\n"
        "    return app\n"
    )
    project = _setup_gate_scenario(
        tmp_path,
        deliverable_content=vulnerable_content,
        verdict_type="APPROVED",
        create_test_file=True,
    )
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"
    reason = str(decision.reason)
    assert "security finding HARDCODED_SECRET_FALLBACK" in reason
    assert "security finding DEBUG_MODE_ENABLED" in reason
    assert "dev-secret-key-12345" in reason

    # Clean deliverable passes the scan: no security findings in the reason
    clean_project = _setup_gate_scenario(
        tmp_path / "clean",
        deliverable_content="import os\nSECRET_KEY = os.environ['SECRET_KEY']\n",
        verdict_type="APPROVED",
        create_test_file=True,
    )
    clean_decision = D024Gate().evaluate_work_order(clean_project, "WO-102")
    assert clean_decision.passed is True
    assert "security finding" not in str(clean_decision.reason)
