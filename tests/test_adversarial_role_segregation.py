"""Adversarial security and role segregation verification tests.

Verifies:
1. Role Segregation Enforced at Kernel Level:
   - Codex attempting git_commit / git_push raises PermissionError.
   - Gemma attempting apply_patch / write_file on application code raises PermissionError.
   - Codex & Gemini attempting create_work_order or authoring contracts raises PermissionError.
2. Diff Hunk Header Pre-validation:
   - An adversarial patch whose hunk header declares a file outside the contract scope is rejected
     BEFORE any file on disk is read or modified.
3. ProcessManager Sandbox Hardening:
   - Env scrubbing (drops parent secrets).
   - Interpreter denylist (blocks interactive python).
   - Path escape validation (blocks ../ and absolute paths).
4. GitOps D025 Safeguards on git_push:
   - Fails closed without explicit confirmation and CEO approval.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession,
    ContractNormalizer,
    OperationJournal,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
    WorkspaceEscapeError,
)
from validators.kernel.identity import get_role_policy
from validators.kernel.process import ProcessManager


def _build_gateway(tmp_path: Path, role: str, allow_patterns: list[str] | None = None) -> tuple[ToolGateway, ScratchWorkspace, Path]:
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "sample.py").write_text("class Original:\n    pass\n", encoding="utf-8")

    workspace = ScratchWorkspace.create(authoritative, f"attempt-{role}")
    contract = ContractNormalizer.normalize({
        "agent": role,
        "wo": "WO-999",
        "scope": {
            "allow": allow_patterns or ["workspace/**", "graph/**"],
            "deny": ["authoritative/**"],
            "write": "read-write",
        },
        "budget": {"max_tokens": 5000, "max_files_touched": 10},
    })
    policy = get_role_policy(role)
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession(role, "mock-provider", str(authoritative), session_id=f"session-{role}")
    attempt = session.create_attempt(contract, attempt_id=f"attempt-{role}")

    tool_gw = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id=f"session-{role}",
        attempt_id=f"attempt-{role}",
        actor_id=role,
        provider_id="mock-provider",
    )
    return tool_gw, workspace, authoritative


# -----------------------------------------------------------------------------
# 1. Role Segregation Tests
# -----------------------------------------------------------------------------

def test_codex_cannot_git_commit_or_git_push(tmp_path: Path) -> None:
    """Codex attempting git_commit or git_push must raise PermissionError."""
    gateway, _, _ = _build_gateway(tmp_path, "codex")

    with pytest.raises(PermissionError) as exc_commit:
        gateway.git_commit("malicious commit")
    assert "denies" in str(exc_commit.value).lower() or "not permitted" in str(exc_commit.value).lower() or "denied" in str(exc_commit.value).lower()

    with pytest.raises(PermissionError) as exc_push:
        gateway.git_push("origin", "main", confirm_push=True)
    assert "denies" in str(exc_push.value).lower() or "not permitted" in str(exc_push.value).lower() or "denied" in str(exc_push.value).lower()


def test_gemma_cannot_apply_patch_or_write_file(tmp_path: Path) -> None:
    """Gemma (QA Lead) attempting apply_patch or write_file on source must raise PermissionError."""
    gateway, workspace, _ = _build_gateway(tmp_path, "gemma")

    # Attempting write_file
    with pytest.raises(PermissionError) as exc_write:
        gateway.write_file("sample.py", "malicious write")
    assert "denies" in str(exc_write.value).lower() or "not permitted" in str(exc_write.value).lower() or "denied" in str(exc_write.value).lower()

    # Attempting apply_patch
    patch_text = (
        "--- sample.py\n"
        "+++ sample.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-class Original:\n"
        "+class Hacked:\n"
        "     pass\n"
    )
    with pytest.raises(PermissionError) as exc_patch:
        gateway.apply_patch("sample.py", patch_text)
    assert "denies" in str(exc_patch.value).lower() or "not permitted" in str(exc_patch.value).lower() or "denied" in str(exc_patch.value).lower()


def test_codex_and_gemini_cannot_create_work_order_or_author_contracts(tmp_path: Path) -> None:
    """Codex and Gemini attempting create_work_order or authoring contracts must raise PermissionError."""
    for role in ("codex", "gemini"):
        gateway, workspace, _ = _build_gateway(tmp_path, role)

        # 1. Attempting create_work_order
        with pytest.raises(PermissionError) as exc_wo:
            gateway.create_work_order(
                wo_id="WO-998",
                title="Unauthorized WO",
                deliverable="src/file.py",
                assigned_agent=role,
            )
        assert "denies" in str(exc_wo.value).lower() or "not permitted" in str(exc_wo.value).lower() or "denied" in str(exc_wo.value).lower()

        # 2. Attempting to write a contract YAML directly
        with pytest.raises(PermissionError) as exc_contract:
            gateway.write_file(".sync/contracts/WO-998.yaml", "work_order: WO-998\n")
        assert "permissionerror" in type(exc_contract.value).__name__.lower() or "denied" in str(exc_contract.value).lower() or "author" in str(exc_contract.value).lower()


# -----------------------------------------------------------------------------
# 2. Diff Hunk Header Pre-validation Tests
# -----------------------------------------------------------------------------

def test_apply_patch_rejects_out_of_scope_hunk_header_before_modifying(tmp_path: Path) -> None:
    """An adversarial patch whose hunk header targets a path outside contract scope is rejected before modifying disk."""
    # Scope allows only workspace/allowed.py; forbidden.py is strictly out of scope
    gateway, workspace, authoritative = _build_gateway(
        tmp_path, "codex", allow_patterns=["workspace/allowed.py"]
    )
    allowed_file = workspace.path_for("allowed.py")
    allowed_file.write_text("def allowed_func():\n    return 42\n", encoding="utf-8")
    original_allowed_text = allowed_file.read_text(encoding="utf-8")

    forbidden_file = workspace.path_for("forbidden.py")
    forbidden_file.write_text("SECRET_KEY = 'super-secret'\n", encoding="utf-8")
    original_forbidden_text = forbidden_file.read_text(encoding="utf-8")

    # Adversarial diff: targets allowed.py, but hunk header references forbidden.py
    adversarial_patch = (
        "--- a/allowed.py\n"
        "+++ b/forbidden.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-SECRET_KEY = 'super-secret'\n"
        "+SECRET_KEY = 'compromised'\n"
    )

    with pytest.raises(PermissionError) as exc_info:
        gateway.apply_patch("allowed.py", adversarial_patch)

    err_msg = str(exc_info.value)
    assert "outside allowed contract scope" in err_msg or "denied" in err_msg.lower()
    assert "forbidden.py" in err_msg

    # Verify BOTH files on disk were COMPLETELY UNTOUCHED
    assert allowed_file.read_text(encoding="utf-8") == original_allowed_text
    assert forbidden_file.read_text(encoding="utf-8") == original_forbidden_text


# -----------------------------------------------------------------------------
# 3. ProcessManager Sandbox Hardening Tests
# -----------------------------------------------------------------------------

def test_process_manager_applies_all_three_sandbox_hardening_layers(tmp_path: Path) -> None:
    """ProcessManager applies env scrubbing, interpreter denylist, and escape validation."""
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    workspace = ScratchWorkspace.create(authoritative, "attempt-proc-test")
    pm = ProcessManager(workspace)

    # Layer 1: Interpreter denylist (blocks shells and inline code execution)
    with pytest.raises(PermissionError) as exc_denylist:
        pm.start_process(["bash", "-c", "echo 1"])
    assert "denied" in str(exc_denylist.value).lower()

    with pytest.raises(PermissionError) as exc_py_c:
        pm.start_process(["python", "-c", "print('blocked')"])
    assert "denied" in str(exc_py_c.value).lower()

    # Layer 2: Workspace escape validation (blocks traversal)
    with pytest.raises(WorkspaceEscapeError):
        pm.start_process(["ls", "../outside"])

    # Layer 3: Environment scrubbing
    os.environ["STACKMIND_SECRET_KEY_123"] = "super-confidential-secret"
    try:
        # Run a python script file that checks if parent secret is present
        script_file = workspace.root / "check_env.py"
        script_file.write_text(
            "import os\n"
            "print('SECRET_FOUND' if 'STACKMIND_SECRET_KEY_123' in os.environ else 'SECRET_SCRUBBED')\n",
            encoding="utf-8",
        )
        proc_id = pm.start_process(["python", "check_env.py"])
        # Wait up to 5 seconds for output
        out = ""
        for _ in range(50):
            out = pm.get_output(proc_id)
            if "SECRET" in out:
                break
            import time; time.sleep(0.1)
        pm.stop_process(proc_id)
        assert "SECRET_SCRUBBED" in out
        assert "SECRET_FOUND" not in out
    finally:
        os.environ.pop("STACKMIND_SECRET_KEY_123", None)
        pm.cleanup_all()


# -----------------------------------------------------------------------------
# 4. GitOps D025 Safeguard Tests
# -----------------------------------------------------------------------------

def test_git_push_d025_safeguard_enforcement(tmp_path: Path) -> None:
    """git_push fails closed without explicit confirm_push and CEO approval receipt."""
    gateway, workspace, _ = _build_gateway(tmp_path, "local-llm")

    # 1. Calling git_push without confirm_push raises D025 PermissionError
    with pytest.raises(PermissionError) as exc_unconfirmed:
        gateway.git_push("origin", "main", confirm_push=False)
    assert "D025" in str(exc_unconfirmed.value)
    assert "confirm_push=True" in str(exc_unconfirmed.value)

    # 2. Calling git_push with confirm_push=True but without CEO receipt raises D025 PermissionError
    with pytest.raises(PermissionError) as exc_unapproved:
        gateway.git_push("origin", "main", confirm_push=True)
    assert "D025" in str(exc_unapproved.value)
    assert "CEO approval receipt" in str(exc_unapproved.value)

    # 3. Adversarial attempt: LLM supplies spoofed 'approved_by="CEO"' argument via tool call
    from unittest.mock import MagicMock
    from validators.kernel.providers.gateway import ProviderGateway, ToolCallRequest
    from validators.kernel.providers.adapter import ProviderAdapter

    prov_gw = ProviderGateway(adapter=MagicMock(spec=ProviderAdapter), tool_gateway=gateway)
    # The tool signature does not accept approved_by, and execution fails closed
    resp = prov_gw.execute_tool_call(
        ToolCallRequest(id="1", name="git_push", arguments={"remote": "origin", "confirm_push": True, "approved_by": "CEO"})
    )
    # Returns error string from execute_tool_call exception handler
    assert "unexpected keyword" in resp or "D025" in resp or "Error" in resp

    # 4. Legitimate operator gate: placing .sync/inbox/CEO/PUSH_APPROVED receipt unlocks push
    ceo_inbox = workspace.root / ".sync" / "inbox" / "CEO"
    ceo_inbox.mkdir(parents=True, exist_ok=True)
    (ceo_inbox / "PUSH_APPROVED").write_text("APPROVED_BY_CEO\n")

    # With receipt present, D025 check passes (subcommand executes)
    # Mock _run_git so we don't attempt a real network push in unit test
    import subprocess
    gateway._run_git = lambda cmd: subprocess.CompletedProcess(cmd, returncode=0, stdout="Pushed to origin/main")
    res = gateway.git_push("origin", "main", confirm_push=True)
    assert res["success"] is True


# -----------------------------------------------------------------------------
# 5. QA Governed Execution & Event Attribution Tests
# -----------------------------------------------------------------------------

def test_gemma_qa_tools_segregation() -> None:
    """Gemma receives exclusively QA and inspection tools, never source code mutations."""
    from validators.kernel.providers.gateway import get_tools_for_role

    gemma_tools = get_tools_for_role("gemma")
    gemma_tool_names = {t.name for t in gemma_tools}

    # Must contain QA & validation tools
    assert "run_tests" in gemma_tool_names
    assert "verify_tests" in gemma_tool_names
    assert "verify_deliverable" in gemma_tool_names
    assert "verify_diff" in gemma_tool_names
    assert "verify_provenance" in gemma_tool_names
    assert "submit_verdict" in gemma_tool_names
    assert "approve_work_order" in gemma_tool_names
    assert "request_changes" in gemma_tool_names
    assert "read_file" in gemma_tool_names
    assert "list_directory" in gemma_tool_names

    # Must NEVER contain code write or git mutation tools
    assert "write_file" not in gemma_tool_names
    assert "apply_patch" not in gemma_tool_names
    assert "delete_file" not in gemma_tool_names
    assert "move_file" not in gemma_tool_names
    assert "git_commit" not in gemma_tool_names
    assert "git_push" not in gemma_tool_names
    assert "create_work_order" not in gemma_tool_names


def test_gemma_can_run_tests_and_submit_verdict(tmp_path: Path) -> None:
    """Gemma can execute run_tests, verify_deliverable, and submit_verdict without scope denial."""
    # Build Gemma gateway with standard application scope and read-only write_mode
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "app").mkdir(parents=True, exist_ok=True)
    (authoritative / "app" / "rate_limiter.py").write_text("class RateLimiter:\n    pass\n", encoding="utf-8")
    (authoritative / "tests").mkdir(parents=True, exist_ok=True)
    (authoritative / "tests" / "test_rate_limiter.py").write_text("def test_dummy():\n    assert True\n", encoding="utf-8")

    workspace = ScratchWorkspace.create(authoritative, "attempt-gemma-qa")
    contract = ContractNormalizer.normalize({
        "agent": "gemma",
        "wo": "WO-002",
        "scope": {
            "allow": ["app/**", "tests/**"],
            "deny": [".git/**", ".sync/inbox/CEO/**"],
            "write": "read-only",
        },
        "budget": {"max_tokens": 10000, "max_files_touched": 10},
    })
    policy = get_role_policy("gemma")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession("gemma", "mock-provider", str(authoritative), session_id="session-gemma")
    session.create_attempt(contract, attempt_id="attempt-gemma")

    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-gemma",
        attempt_id="attempt-gemma",
        actor_id="gemma",
        provider_id="mock-provider",
    )

    # 1. run_tests against tests suite
    # Mock sandbox.run to simulate clean pytest outcome
    import subprocess
    gateway.sandbox.run = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode=0, stdout="1 passed", stderr="")
    test_res = gateway.run_tests("tests")
    assert test_res["success"] is True

    # 2. submit_verdict to Claude
    msg = gateway.submit_verdict(
        wo_id="WO-001",
        verdict="APPROVED",
        report="All rate limiter unit tests pass with 100% assertion coverage.",
        metrics={"coverage": 1.0, "tests_passed": 1},
    )
    assert "APPROVED" in msg
    verdict_file = workspace.root / ".sync" / "inbox" / "claude" / "verdict_WO-001.md"
    assert verdict_file.is_file()
    assert "QA VERDICT: APPROVED" in verdict_file.read_text(encoding="utf-8")

    # 3. approve_work_order
    appr_msg = gateway.approve_work_order(wo_id="WO-001")
    assert "APPROVED" in appr_msg


def test_complete_operation_preserves_role_and_agent(tmp_path: Path) -> None:
    """Daemon complete_operation emits role, agent_id, and operation in events."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon.json")
    mgr = SessionManager(storage)
    contract = {"agent": "gemma", "wo": "WO-002", "scope": {"allow": ["tests/**"], "deny": []}}
    session = mgr.create_session("gemma", "mock-provider", contract, str(tmp_path))
    session_id = session["session_id"]

    events_received: list[dict[str, Any]] = []
    mgr.events.subscribe(lambda e: events_received.append(e.as_dict()))

    # Begin operation as QA
    _, op_id = mgr.begin_operation(session_id, "qa.verify", role="qa", agent_id="gemma", work_order_id="WO-002")

    # Fail the operation
    mgr.complete_operation(session_id, op_id, {"error": "test failure"}, status="FAILED")

    failed_event = next(e for e in events_received if e["name"] == "operation.failed")
    assert failed_event["payload"]["role"] == "qa"
    assert failed_event["payload"]["agent_id"] == "gemma"
    assert failed_event["payload"]["work_order_id"] == "WO-002"
    assert failed_event["payload"]["operation"] == "qa.verify"


def test_gemma_qa_outcome_verified_without_code_deliverable(tmp_path: Path) -> None:
    """Gemma QA work order with doc/verification deliverable passes outcome_verified."""
    from validators.harness.runner import AgentRunner, HarnessTask, HarnessDecision
    from validators.harness.snapshot import WorkspaceSnapshot

    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (project_root / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    runner = AgentRunner(project_root, agent="gemma")
    task = HarnessTask(
        kind="work_order",
        identifier="WO-003",
        path=project_root / ".sync" / "work-orders" / "ACTIVE" / "WO-003.yaml",
        title="QA verification and sign-off",
        body="Verify all unit tests pass and approve the release",
        query="QA verification",
        work_order_id="WO-003",
        deliverable_path=None,
    )
    decision = HarnessDecision(
        status="completed",
        summary="All tests passed with 100% assertions satisfied. QA sign-off granted.",
        report_markdown="# QA Report\n\nVerified test suite.",
        blockers=(),
        modified_files=(),
        release_target="WO-003",
        retrieval_queries=(),
        uncertainty=(),
        commands=(),
    )

    before_snap = WorkspaceSnapshot.capture(project_root)
    after_snap = WorkspaceSnapshot.capture(project_root)
    diff = before_snap.diff(after_snap)

    dimensions = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=project_root,
        staged_errors=[],
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=False,
    )

    assert dimensions.outcome_verified is True
    assert dimensions.all_passed is True


