"""Tests for Phase 3 Process Lifecycle Management and Specialized Execution Tools."""

import sys
import time
from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "codex"):
    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": ["**"],
            "denied": [],
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


def test_process_lifecycle(tmp_path):
    gateway, journal = _setup_gateway(tmp_path, "codex")

    # Create a small python script that emits output and sleeps
    script = tmp_path / "worker.py"
    script.write_text(
        "import time, sys\n"
        "print('HELLO_START', flush=True)\n"
        "time.sleep(2)\n"
        "print('HELLO_END', flush=True)\n",
        encoding="utf-8"
    )

    proc_id = gateway.process_start([sys.executable, "worker.py"])
    assert proc_id.startswith("proc-")

    # Check status
    time.sleep(0.3)
    status = gateway.process_status(proc_id)
    assert status["status"] in ("running", "exited")
    assert status["process_id"] == proc_id

    # Read output
    output = gateway.process_output(proc_id)
    assert "HELLO_START" in output

    # Stop process
    stopped = gateway.process_stop(proc_id, timeout=2.0)
    assert stopped is True

    final_status = gateway.process_status(proc_id)
    assert final_status["status"] in ("stopped", "exited")

    # Cleanup guarantee
    gateway.cleanup()


def test_process_cleanup_guarantee(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex")
    script = tmp_path / "sleep_forever.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    proc_id = gateway.process_start([sys.executable, "sleep_forever.py"])
    status = gateway.process_status(proc_id)
    assert status["status"] == "running"

    # Cleanup should kill it immediately
    gateway.cleanup()
    time.sleep(0.2)
    after = gateway.process_status(proc_id)
    assert after["status"] in ("stopped", "exited")


def test_process_denylist_enforcement(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex")
    # Denied command string code execution
    with pytest.raises(PermissionError, match="invoked with code-string flag|Interpreter denylist|denied"):
        gateway.process_start([sys.executable, "-c", "print('forbidden')"])


def test_role_enforcement_for_process_tools(tmp_path):
    # Claude has '—' for process_start (strictly forbidden)
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.process_start([sys.executable, "test.py"])

    # Claude also cannot run tests
    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.run_tests()


def test_run_lint_and_security_scan(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex")

    # Valid syntax
    good = tmp_path / "good.py"
    good.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    lint_res = gateway.run_lint("good.py")
    assert lint_res["success"] is True
    assert len(lint_res["errors"]) == 0

    # Syntax error
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    lint_res = gateway.run_lint("bad.py")
    assert lint_res["success"] is False
    assert len(lint_res["errors"]) == 1

    # Security scan - clean file
    sec_clean = gateway.run_security_scan("good.py")
    assert sec_clean["success"] is True

    # Security scan - dangerous code
    danger = tmp_path / "danger.py"
    danger.write_text("token = 'secret_key_1234567890123456'\neval('foo')\n", encoding="utf-8")
    sec_danger = gateway.run_security_scan("danger.py")
    assert sec_danger["success"] is False
    assert len(sec_danger["findings"]) >= 1
