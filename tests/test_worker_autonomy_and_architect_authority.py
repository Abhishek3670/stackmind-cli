from pathlib import Path
import tempfile
import yaml
import pytest

from validators.harness.contract_gate import verify_post_execution, ContractAccessDenied
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_readiness import validate_authoring_readiness, _scope_allows
from validators.knowledge.contract import AgentContract


def test_contract_schema_without_allow_deny():
    """Verify that contract schema accepts contracts without explicit allow and deny lists."""
    contract_data = {
        "schema_version": 1,
        "agent_id": "gemini",
        "work_order": "WO-001",
        "identity": {"role": "frontend"},
        "scope": {"write": "read-write"},
        "budget": {"max_files_touched": 5},
    }
    gate = AuthoringGate()
    decision = gate.validate_artifact_content(
        ".sync/contracts/WO-001.yaml",
        yaml.safe_dump(contract_data),
        agent="claude",
    )
    assert decision.passed, f"Validation failed: {decision.errors}"


def test_worker_can_write_own_files_without_scope_allow():
    """Verify specialized workers are free to write their own files when scope.allow is omitted."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        contracts_dir = ws / ".sync" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        wos_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        wos_dir.mkdir(parents=True, exist_ok=True)

        contract_data = {
            "schema_version": 1,
            "agent_id": "gemini",
            "work_order": "WO-001",
            "identity": {"role": "frontend"},
            "scope": {"write": "read-write"},
            "budget": {"max_files_touched": 10},
        }
        (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(contract_data), encoding="utf-8")

        wo_data = {
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Frontend Scaffolding",
            "priority": "P0",
            "dependencies": [],
            "assigned_agents": ["gemini"],
            "deliverable": {
                "type": "config",
                "path": "index.html",
                "description": "Semantic HTML shell",
            },
            "status": "ACTIVE",
        }
        (wos_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

        class DummyTask:
            work_order_id = "WO-001"

        class DummyDecision:
            modified_files = ["index.html", "assets/css/style.css", "assets/js/app.js"]

        # Gemini touches multiple files for its own frontend deliverable without scope.allow:
        verify_post_execution(
            ws,
            agent="gemini",
            task=DummyTask(),
            decision=DummyDecision(),
            observed_files=DummyDecision.modified_files,
        )


def test_worker_cannot_modify_other_workers_work():
    """Verify that a worker attempting to modify another worker's deliverable is blocked."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        contracts_dir = ws / ".sync" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        wos_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        wos_dir.mkdir(parents=True, exist_ok=True)

        # Peer work order owned by gemini (frontend)
        gemini_wo = {
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Frontend Scaffolding",
            "priority": "P0",
            "dependencies": [],
            "assigned_agents": ["gemini"],
            "deliverable": {
                "type": "config",
                "path": "index.html",
                "description": "Frontend index.html",
            },
            "status": "ACTIVE",
        }
        (wos_dir / "WO-001.yaml").write_text(yaml.safe_dump(gemini_wo), encoding="utf-8")

        # Current work order owned by codex (backend)
        codex_contract = {
            "schema_version": 1,
            "agent_id": "codex",
            "work_order": "WO-002",
            "identity": {"role": "backend"},
            "scope": {"write": "read-write"},
            "budget": {"max_files_touched": 5},
        }
        (contracts_dir / "WO-002.yaml").write_text(yaml.safe_dump(codex_contract), encoding="utf-8")

        codex_wo = {
            "id": "WO-002",
            "type": "FEATURE",
            "title": "Backend API",
            "priority": "P1",
            "dependencies": ["WO-001"],
            "assigned_agents": ["codex"],
            "deliverable": {
                "type": "code",
                "path": "src/api.py",
                "description": "Backend routes and API",
            },
            "status": "ACTIVE",
        }
        (wos_dir / "WO-002.yaml").write_text(yaml.safe_dump(codex_wo), encoding="utf-8")

        class DummyTask:
            work_order_id = "WO-002"

        # Codex attempts to modify Gemini's index.html
        class ClobberDecision:
            modified_files = ["src/api.py", "index.html"]

        with pytest.raises(ContractAccessDenied) as exc_info:
            verify_post_execution(
                ws,
                agent="codex",
                task=DummyTask(),
                decision=ClobberDecision(),
                observed_files=ClobberDecision.modified_files,
            )
        assert "owned by another work order" in str(exc_info.value)
        assert "Workers cannot modify each other's work" in str(exc_info.value)


def test_architect_full_control_to_cancel_and_supersede():
    """Verify Architect can freely cancel, supersede, and amend work orders without overwrite conflicts."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        wos_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        wos_dir.mkdir(parents=True, exist_ok=True)

        initial_wo = {
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Initial Task",
            "status": "ACTIVE",
            "priority": "P1",
            "revision": 1,
            "assigned_agents": ["gemini"],
            "dependencies": [],
            "deliverable": {
                "type": "code",
                "path": "index.html",
                "description": "Initial HTML deliverable",
            },
        }
        (wos_dir / "WO-001.yaml").write_text(yaml.safe_dump(initial_wo), encoding="utf-8")

        gate = AuthoringGate(project_root=ws)

        # 1. Architect cancels the work order
        cancelled_wo = dict(initial_wo)
        cancelled_wo["status"] = "CANCELLED"
        cancelled_wo["assigned_agents"] = []
        dec = gate.validate_artifact_content(
            ".sync/work-orders/ACTIVE/WO-001.yaml",
            yaml.safe_dump(cancelled_wo),
            agent="claude",
            project_root=ws,
        )
        assert dec.passed, f"Architect cancel rejected: {dec.errors}"

        # 2. Architect supersedes the work order
        superseded_wo = dict(initial_wo)
        superseded_wo["status"] = "SUPERSEDED"
        superseded_wo["superseded_by"] = "WO-005"
        dec2 = gate.validate_artifact_content(
            ".sync/work-orders/ACTIVE/WO-001.yaml",
            yaml.safe_dump(superseded_wo),
            agent="claude",
            project_root=ws,
        )
        assert dec2.passed, f"Architect supersede rejected: {dec2.errors}"
