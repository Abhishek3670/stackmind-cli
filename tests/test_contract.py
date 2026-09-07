from __future__ import annotations
import shutil
from pathlib import Path
import pytest
import yaml
from click.testing import CliRunner
from validators.knowledge.contract import AgentContract, ContractValidationError, ContractAccessDenied, ContractExpiredError, path_to_module, module_matches
from validators.knowledge.compiler.ir import CompilerIR, SymbolIR, EdgeIR
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.compiler import compile_project
from validators.knowledge.writer import write_knowledge
from validators.knowledge.projections import build_projections
from cli.init import init
from cli.main import cli

@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    # Copy schemas directory to fresh_project so they are available for contract validation tests
    src_schemas = Path(__file__).parent.parent / "schemas"
    shutil.copytree(src_schemas, project / "schemas")
    return project

@pytest.fixture
def runner():
    return CliRunner()

def put(project: Path, name: str, text: str) -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')

def build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)

def test_path_to_module():
    assert path_to_module("billing/invoices.py") == "billing.invoices"
    assert path_to_module("billing/invoices/__init__.py") == "billing.invoices"
    assert path_to_module("./billing/invoices/core.py") == "billing.invoices.core"

def test_module_matches():
    assert module_matches("auth.session", "auth.*")
    assert module_matches("auth.session.login", "auth.*")
    assert module_matches("billing.invoices", "billing.invoices")
    assert module_matches("billing.invoices.core", "billing.invoices")
    assert not module_matches("auth_helpers", "auth.*")

def test_contract_validation(fresh_project):
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "identity": {
            "role": "implementer",
            "reports_to": "senior-architect"
        },
        "scope": {
            "allow": [
                {"module": "billing.invoices", "depth": 2},
                {"module": "billing.tests"}
            ],
            "deny": [
                {"module": "auth.*"}
            ],
            "write": "read-write"
        },
        "budget": {
            "max_files_touched": 6,
            "max_tokens": 40000,
            "expires_at": "2026-07-22T18:00:00Z"
        }
    }
    
    # Write contract to yaml
    contract_file = fresh_project / "contract.yaml"
    with open(contract_file, "w", encoding="utf-8") as f:
        yaml.dump(contract_data, f)
        
    contract = AgentContract.load(contract_file, fresh_project)
    assert contract.agent_id == "agent-codex-07"
    assert contract.work_order == "WO-142"
    assert contract.write_mode == "read-write"
    
    # Test top-level wrapping key
    wrapped_data = {"contract": contract_data}
    wrapped_file = fresh_project / "wrapped_contract.yaml"
    with open(wrapped_file, "w", encoding="utf-8") as f:
        yaml.dump(wrapped_data, f)
        
    contract_wrapped = AgentContract.load(wrapped_file, fresh_project)
    assert contract_wrapped.agent_id == "agent-codex-07"

def test_invalid_contract_raises_error(fresh_project):
    invalid_data = {
        "agent_id": "agent-codex-07",
        # Missing required fields like work_order, scope, budget
    }
    contract_file = fresh_project / "invalid_contract.yaml"
    with open(contract_file, "w", encoding="utf-8") as f:
        yaml.dump(invalid_data, f)
        
    with pytest.raises(ContractValidationError):
        AgentContract.load(contract_file, fresh_project)

def test_contract_expiration():
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "scope": {
            "allow": [],
            "deny": [],
            "write": "read-only"
        },
        "budget": {
            "expires_at": "2026-07-22T18:00:00Z"
        }
    }
    contract = AgentContract(contract_data)
    # 2026-07-22T17:00:00Z is not expired
    assert not contract.is_expired("2026-07-22T17:00:00Z")
    # 2026-07-22T19:00:00Z is expired
    assert contract.is_expired("2026-07-22T19:00:00Z")

def test_is_node_in_scope():
    ir = CompilerIR(
        revision_inputs={},
        symbols=[
            SymbolIR(node_id="FUNC-1", kind="Function", path="billing/invoices.py", qualified_name="billing.invoices.create", signature="", location={}, content_hash="1"),
            SymbolIR(node_id="FUNC-2", kind="Function", path="billing/tests.py", qualified_name="billing.tests.test_all", signature="", location={}, content_hash="2"),
            SymbolIR(node_id="FUNC-3", kind="Function", path="auth/session.py", qualified_name="auth.session.login", signature="", location={}, content_hash="3"),
            SymbolIR(node_id="FUNC-4", kind="Function", path="billing/helpers.py", qualified_name="billing.helpers.format", signature="", location={}, content_hash="4"),
            SymbolIR(node_id="FUNC-5", kind="Function", path="billing/deep.py", qualified_name="billing.deep.very_deep", signature="", location={}, content_hash="5"),
        ],
        edges=[
            # FUNC-1 calls FUNC-4 (depth 1 from billing.invoices)
            EdgeIR(source_id="FUNC-1", target_id="FUNC-4", relation="CALLS", target_name="format", resolution="RESOLVED", confidence=1.0, path="billing/invoices.py", line=1),
            # FUNC-4 calls FUNC-5 (depth 2 from billing.invoices)
            EdgeIR(source_id="FUNC-4", target_id="FUNC-5", relation="CALLS", target_name="very_deep", resolution="RESOLVED", confidence=1.0, path="billing/helpers.py", line=1),
        ]
    )
    
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "scope": {
            "allow": [
                {"module": "billing.invoices", "depth": 1},
                {"module": "billing.tests", "depth": 0}
            ],
            "deny": [
                {"module": "auth.*"}
            ]
        },
        "budget": {}
    }
    contract = AgentContract(contract_data)
    
    # FUNC-1 is in billing.invoices -> direct allow
    assert contract.is_node_in_scope("FUNC-1", ir)
    # FUNC-2 is in billing.tests -> direct allow
    assert contract.is_node_in_scope("FUNC-2", ir)
    # FUNC-3 is in auth.session -> denied
    assert not contract.is_node_in_scope("FUNC-3", ir)
    # FUNC-4 is depth 1 from FUNC-1 -> allowed (depth is 1)
    assert contract.is_node_in_scope("FUNC-4", ir)
    # FUNC-5 is depth 2 from FUNC-1 -> denied (depth is 1 max)
    assert not contract.is_node_in_scope("FUNC-5", ir)

def test_knowledge_api_contract_enforcement(fresh_project):
    put(
        fresh_project,
        "app.py",
        "def helper_one(value):\n"
        "    return value + 1\n\n"
        "def helper_two(value):\n"
        "    return helper_one(value)\n"
    )
    put(
        fresh_project,
        "auth.py",
        "def login(user):\n"
        "    return True\n"
    )
    build_graph(fresh_project)
    
    # 1. Create a valid contract allowing only "app"
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": [
                {"module": "auth.*"}
            ]
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z"
        }
    }
    
    contract_file = fresh_project / "contract.yaml"
    with open(contract_file, "w", encoding="utf-8") as f:
        yaml.dump(contract_data, f)
        
    api = KnowledgeAPI(fresh_project, contract=contract_file)
    
    # helper_one is inside allowed module "app" -> should work
    res = api.lookup("helper_one")
    assert res.results
    assert res.results[0].qualified_name == "helper_one"
    
    # login is inside denied module "auth" -> should raise ContractAccessDenied
    with pytest.raises(ContractAccessDenied):
        api.lookup("login")
        
    # 2. Expiration check
    expired_contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": []
        },
        "budget": {
            "expires_at": "2020-07-22T18:00:00Z" # Already expired
        }
    }
    
    expired_contract_file = fresh_project / "expired_contract.yaml"
    with open(expired_contract_file, "w", encoding="utf-8") as f:
        yaml.dump(expired_contract_data, f)
        
    api_expired = KnowledgeAPI(fresh_project, contract=expired_contract_file)
    
    with pytest.raises(ContractExpiredError):
        api_expired.lookup("helper_one")

def test_contract_cli_commands(fresh_project, runner):
    put(
        fresh_project,
        "app.py",
        "def helper_one(value):\n"
        "    return value + 1\n"
    )
    build_graph(fresh_project)
    
    contract_data = {
        "agent_id": "agent-codex-07",
        "work_order": "WO-142",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": [
                {"module": "auth.*"}
            ],
            "write": "read-write"
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z"
        }
    }
    
    contracts_dir = fresh_project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract_file = contracts_dir / "WO-142.yaml"
    with open(contract_file, "w", encoding="utf-8") as f:
        yaml.dump(contract_data, f)
        
    # 1. Test "graph contract show"
    result = runner.invoke(cli, ["graph", "contract", "show", "WO-142", "-p", str(fresh_project)])
    assert result.exit_code == 0
    assert "agent-codex-07" in result.output
    assert "WO-142" in result.output
    
    # 2. Test "graph contract validate" (allowed edit)
    result_edit_ok = runner.invoke(cli, ["graph", "contract", "validate", "WO-142", "--op", "edit app.py", "-p", str(fresh_project)])
    assert result_edit_ok.exit_code == 0
    assert "ALLOWED" in result_edit_ok.output
    
    # 3. Test "graph contract validate" (denied edit)
    result_edit_deny = runner.invoke(cli, ["graph", "contract", "validate", "WO-142", "--op", "edit auth.py", "-p", str(fresh_project)])
    assert result_edit_deny.exit_code != 0
    assert "REJECTED" in result_edit_deny.output
    
    # 4. Test "graph explain-denial"
    result_explain_allow = runner.invoke(cli, ["graph", "explain-denial", "WO-142", "--node", "helper_one", "-p", str(fresh_project)])
    assert result_explain_allow.exit_code == 0
    assert "ALLOWED" in result_explain_allow.output
    
    # 5. Test "graph scope"
    result_scope = runner.invoke(cli, ["graph", "scope", "agent-codex-07", "-p", str(fresh_project)])
    assert result_scope.exit_code == 0
    assert "app" in result_scope.output
