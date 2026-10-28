from __future__ import annotations

import sys
from pathlib import Path
import pytest
import yaml

from validators.kernel import (
    AgentSession,
    ContractEvaluator,
    ContractNormalizer,
    OperationJournal,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
)
from validators.kernel.sandbox import ProcessSandbox
from validators.kernel.identity import get_role_policy
from validators.kernel.permission_matrix import get_permission_matrix_prompt
from validators.harness.task_contract import compile_worker_task_contract, render_worker_system


def test_qa_agent_read_and_list_authorization():
    """QA verifiers (gemma / qa) have full read and list access across workspace folders (server/, src/, css/)

    while write operations remain strictly isolated to tests/ and inbox.
    """
    qa_contract_dict = {
        "agent_id": "gemma",
        "work_order": "WO-035",
        "identity": {"role": "qa"},
        "scope": {
            "allow": [
                {"module": "tests/**"},
                {"module": ".sync/inbox/claude/**"},
            ],
            "deny": [],
            "write": "read-write",
        },
    }
    norm_qa = ContractNormalizer.normalize(qa_contract_dict)
    evaluator = ContractEvaluator()

    # QA must be authorized to inspect source & asset directories
    for folder in ("server", "src", "css", "assets", "static"):
        authed, reason = evaluator.authorize(norm_qa, "list_directory", folder)
        assert authed is True, f"QA should be authorized to list {folder}, got {reason}"

        authed_glob, reason_glob = evaluator.authorize(norm_qa, "glob", folder)
        assert authed_glob is True, f"QA should be authorized to glob {folder}, got {reason_glob}"

    # QA read_file on application code should be allowed
    authed_read, _ = evaluator.authorize(norm_qa, "read_file", "server/server.py")
    assert authed_read is True

    # QA writing outside tests/ MUST fail closed
    authed_write, _ = evaluator.authorize(norm_qa, "write_file", "server/server.py")
    assert authed_write is False

    authed_patch, _ = evaluator.authorize(norm_qa, "apply_patch", "src/index.html")
    assert authed_patch is False

    authed_patch_f, _ = evaluator.authorize(norm_qa, "patch_file", "src/index.html")
    assert authed_patch_f is False

    # QA writing inside tests/ is allowed
    authed_write_test, _ = evaluator.authorize(norm_qa, "write_file", "tests/test_tts.py")
    assert authed_write_test is True


def test_non_qa_worker_is_denied_listing_unrelated_directories():
    """Regular workers (e.g. backend / codex) are strictly confined to their declared scope and ancestors."""
    backend_contract_dict = {
        "agent_id": "codex",
        "work_order": "WO-030",
        "identity": {"role": "backend"},
        "scope": {
            "allow": [{"module": "server/**"}],
            "deny": [],
            "write": "read-write",
        },
    }
    norm_backend = ContractNormalizer.normalize(backend_contract_dict)
    evaluator = ContractEvaluator()

    # Allowed to list server
    authed_server, _ = evaluator.authorize(norm_backend, "list_directory", "server")
    assert authed_server is True

    # Denied from listing tests or frontend src
    authed_tests, _ = evaluator.authorize(norm_backend, "list_directory", "tests")
    assert authed_tests is False

    authed_src, _ = evaluator.authorize(norm_backend, "list_directory", "src")
    assert authed_src is False


def test_windows_os_command_emulation(tmp_path: Path, monkeypatch):
    """On Windows, ToolGateway natively emulates 'ls', 'ls -la', and 'pwd' to prevent execution crashes."""
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    workspace = ScratchWorkspace.create(authoritative, "attempt-qa")

    # Create directory structure in scratch workspace root
    (workspace.root / "server").mkdir(parents=True, exist_ok=True)
    (workspace.root / "server" / "app.py").write_text("print('hello')", encoding="utf-8")
    (workspace.root / "index.html").write_text("<html></html>", encoding="utf-8")

    contract = ContractNormalizer.normalize({
        "agent": "qa",
        "wo": "WO-035",
        "scope": {"allow": ["tests/**", "workspace/command"], "deny": [], "write": "read-write"},
        "budget": {"max_tokens": 5000, "max_files_touched": 10},
    })
    policy = get_role_policy("qa")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession("qa", "mock-provider", str(authoritative), session_id="session-qa")
    session.create_attempt(contract, attempt_id="attempt-qa")

    tool_gw = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-qa",
        attempt_id="attempt-qa",
        actor_id="qa",
        provider_id="mock-provider",
    )

    monkeypatch.setattr(sys, "platform", "win32")

    # Emulate 'ls' / 'ls -la'
    res_ls = tool_gw.run_command(["ls", "-la"])
    assert res_ls.exit_code == 0
    assert "<DIR>  server" in res_ls.stdout
    assert "index.html" in res_ls.stdout

    # Emulate 'pwd'
    res_pwd = tool_gw.run_command(["pwd"])
    assert res_pwd.exit_code == 0
    assert str(workspace.root) in res_pwd.stdout

    # Non-existent target
    res_ls_missing = tool_gw.run_command(["ls", "does_not_exist"])
    assert res_ls_missing.exit_code == 1
    assert "No such file or directory" in res_ls_missing.stderr


def test_process_sandbox_catches_filenotfound_with_os_guidance(tmp_path: Path):
    """ProcessSandbox catches FileNotFoundError and returns exit code 127 with informative guidance."""
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "sandbox-test")
    sandbox = ProcessSandbox(ws)

    # Execute a completely non-existent binary
    res = sandbox.run(["__stackmind_non_existent_binary_xyz_123__", "--flag"])
    assert res.exit_code == 127
    assert res.returncode == 127
    assert "executable or file not found" in res.stderr
    if sys.platform == "win32":
        assert "Host OS is Windows (win32)" in res.stderr
        assert "Use StackMind built-in tools" in res.stderr


def test_task_contract_and_system_prompt_os_awareness(tmp_path: Path):
    """Task contract compilation and system prompt injection include Host OS context and QA guidance."""
    class DummyTask:
        work_order_id = "WO-035"
        agent_id = "gemma"
        role = "qa"
        deliverable_path = "tests/test_tts.py"
        title = "QA Verification"
        body = "Verify TTS integration"

    wo_data = {
        "id": "WO-035",
        "title": "QA Verification",
        "description": "Verify TTS integration",
    }
    contract_data = {
        "agent_id": "gemma",
        "identity": {"role": "qa"},
        "scope": {"allow": [{"module": "tests/**"}]},
    }

    compiled = compile_worker_task_contract(
        task=DummyTask(),
        wo_data=wo_data,
        contract_data=contract_data,
        workspace=tmp_path,
    )

    assert "host_operating_system" in compiled
    assert "host_shell_environment" in compiled
    assert "qa_read_accessibility" in compiled
    assert "full read-only access" in compiled["qa_read_accessibility"]

    system_prompt = render_worker_system(compiled)
    assert "Host OS:" in system_prompt

    perm_matrix = get_permission_matrix_prompt("qa")
    assert "### 💻 HOST ENVIRONMENT & OPERATING SYSTEM" in perm_matrix
    assert "READ ACCESSIBILITY: You have complete read-only access" in perm_matrix


def test_standard_test_modules_allow_yaml_and_httpx(tmp_path: Path):
    """Test files in tests/ are permitted to import standard test utilities (yaml, httpx, mock)

    even if not declared in requirements.txt, while production files fail closed.
    """
    from validators.harness.dependency_gate import check_import_satisfiability

    # Create empty requirements.txt with only pytest
    (tmp_path / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")

    # 1. Test file importing yaml, pytest, httpx
    test_file = tmp_path / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_code = """
import pytest
import yaml
import httpx

def test_config():
    data = yaml.safe_load("a: 1")
    assert data["a"] == 1
"""
    test_file.write_text(test_code, encoding="utf-8")

    res_test = check_import_satisfiability(test_file, tmp_path)
    assert res_test.passed is True
    assert len(res_test.undeclared_imports) == 0

    # 2. Production file importing yaml without declaration must fail closed
    src_file = tmp_path / "src" / "server.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_code = """
import yaml

def parse():
    return yaml.safe_load("{}")
"""
    src_file.write_text(src_code, encoding="utf-8")

    res_src = check_import_satisfiability(src_file, tmp_path)
    assert res_src.passed is False
    assert "yaml" in res_src.undeclared_imports


def test_architect_recovery_dispatch_recovers_from_operation_contention(tmp_path: Path):
    """_architect_turn clears stale active_operation on contention and succeeds on retry."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        workspace=str(tmp_path),
        product_goal="test",
    )

    class MockManager:
        def __init__(self):
            self.session = {"session_id": "sess-test", "active_operation": "op-stale-worker"}
            self._active = {"op-stale-worker": object()}
            self.start_turn_attempts = 0

        def get_session(self, session_id):
            return self.session

        def cancel_operation(self, op_id):
            if self.session.get("active_operation") == op_id:
                self.session["active_operation"] = None

        def start_turn(self, session_id, prompt, **kwargs):
            self.start_turn_attempts += 1
            if self.session.get("active_operation"):
                raise RuntimeError("OperationContentionError: session already has active operation")
            return {"operation_id": "op-new-architect", "status": "RUNNING"}

    mock_mgr = MockManager()
    sup = LifecycleSupervisor(manager=mock_mgr)

    # Dispatch architect turn
    result = sup._architect_turn(state, "Recovery prompt", "WO-035")

    assert result is not None
    assert result["operation_id"] == "op-new-architect"
    assert mock_mgr.start_turn_attempts == 2
    assert mock_mgr.session["active_operation"] is None
