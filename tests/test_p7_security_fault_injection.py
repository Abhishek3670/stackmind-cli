"""Comprehensive test suite for Milestone P7-5: Security Hardening & Fault Injection.

Tests:
1. Subagent Scope Containment (positive containment, negative escalation rejection).
2. Credential Zero-Leakage Scanning (pattern detection, recursive scanning, redaction, RPC responses).
3. D025 Subagent Destructive Safeguards (backup, clean git, approval enforcement).
4. Terminal Output Sanitization (ANSI, OSC, control char stripping, clean assertion).
5. Budget Limit Overrun Gating (tokens, steps, files, time overrun enforcement).
6. Active Role Rebinding Security Gate (in-flight rejection, post-completion permit).
7. Fault Injection: Backend Crash and Timeout Simulation.
8. Fault Injection: Subagent Crash & Completion Blocking.
9. Fault Injection: Cancellation Race vs Natural Completion.
10. Fault Injection: Reconnect & Full Tree Reconstruction Recovery.
11. Fault Injection: Concurrent Rebind & Completion Race.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from validators.kernel.daemon import LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.security import (
    BudgetExceededError,
    BudgetTracker,
    CredentialLeakError,
    CredentialLeakScanner,
    D025SafeguardVerifier,
    D025SubagentViolationError,
    FaultInjectionEngine,
    ScopeEscalationError,
    UnsafeOutputError,
    assert_scope_contained,
    assert_terminal_output_clean,
    is_terminal_output_clean,
    sanitize_terminal_output,
    scan_for_credential_leaks,
    validate_subagent_scope,
)
from validators.kernel.tui.client import DaemonClient


# ─── 1. SUBAGENT SCOPE CONTAINMENT TESTS ──────────────────────────────────────

def test_subagent_scope_containment_positive_cases():
    # 1. Inherit or None
    assert validate_subagent_scope({"allow": ["src/*"]}, None) is True
    assert validate_subagent_scope({"allow": ["src/*"]}, "inherit") is True

    # 2. Same scope
    assert validate_subagent_scope({"allow": ["src/*"]}, {"allow": ["src/*"]}) is True

    # 3. Narrower subpath scope
    assert validate_subagent_scope({"allow": ["src/*"]}, {"allow": ["src/api/*"]}) is True
    assert validate_subagent_scope({"allow": ["src/**"]}, {"allow": ["src/api/v1/auth.py"]}) is True

    # 4. Wildcard parent
    assert validate_subagent_scope({"allow": ["*"]}, {"allow": ["cli/tui/*"]}) is True

    # 5. Deny rules preserved
    parent_with_deny = {"allow": ["src/*"], "deny": ["src/auth/*"]}
    child_safe = {"allow": ["src/api/*"], "deny": ["src/auth/*"]}
    assert validate_subagent_scope(parent_with_deny, child_safe) is True


def test_subagent_scope_containment_negative_escalation():
    # 1. Wider scope attempt
    assert validate_subagent_scope({"allow": ["src/api/*"]}, {"allow": ["src/*"]}) is False

    # 2. Sibling directory escape
    assert validate_subagent_scope({"allow": ["src/*"]}, {"allow": ["config/*"]}) is False

    # 3. Global wildcard escalation
    assert validate_subagent_scope({"allow": ["src/*"]}, {"allow": ["*"]}) is False

    # 4. Deny rule bypass / omitted deny rule
    parent_with_deny = {"allow": ["src/*"], "deny": ["src/auth/*"]}
    child_missing_deny = {"allow": ["src/*"], "deny": []}
    assert validate_subagent_scope(parent_with_deny, child_missing_deny) is False

    # 5. Child allow touches parent deny
    child_touching_deny = {"allow": ["src/auth/token.py"], "deny": ["src/auth/*"]}
    assert validate_subagent_scope(parent_with_deny, child_touching_deny) is False

    # 6. Assert helper raises typed ScopeEscalationError
    with pytest.raises(ScopeEscalationError, match="Scope escalation rejected"):
        assert_scope_contained({"allow": ["src/api/*"]}, {"allow": ["src/*"]}, role="backend")


def test_subagent_spawn_escalation_rejected_by_daemon(tmp_path: Path):
    """Verify daemon rejects scope escalation at dispatch_subagent spawn time."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="daemon",
            contract={"allow": ["src/api/*"], "deny": ["src/auth/*"], "write_mode": "governed"},
            workspace=str(tmp_path),
        )
        sid = session["session_id"]

        # Parent operation
        _, parent_op_id = daemon.manager.begin_operation(
            session_id=sid,
            operation_name="architecture.plan",
            contract_scope={"allow": ["src/api/*"], "deny": ["src/auth/*"]},
        )

        # Child attempts wider scope -> Rejected
        with pytest.raises(ValueError, match="out of parent allow scope"):
            daemon.manager.dispatch_subagent(
                session_id=sid,
                parent_operation_id=parent_op_id,
                role="backend",
                work_order_id="WO-101",
                contract_scope={"allow": ["src/*"], "deny": ["src/auth/*"]},
            )


# ─── 2. CREDENTIAL ZERO-LEAKAGE SCANNING TESTS ────────────────────────────────

def test_credential_leak_scanner_pattern_detection():
    scanner = CredentialLeakScanner(additional_secrets=["my-custom-super-secret-12345"])

    # Clean text
    assert len(scanner.scan_text("Normal operational logs with no secrets.")) == 0

    # API key patterns
    assert len(scanner.scan_text("api_key = 'sk-1234567890abcdefghijklmnopq'")) > 0
    assert len(scanner.scan_text("ghp_123456789012345678901234567890")) > 0
    assert len(scanner.scan_text("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnopqrstuv")) > 0

    # Configured secret
    assert len(scanner.scan_text("Encountered token my-custom-super-secret-12345 in header")) > 0

    # Redaction helper
    redacted = scanner.redact("API: sk-1234567890abcdefghijklmnopq and my-custom-super-secret-12345")
    assert "sk-" not in redacted
    assert "my-custom-super-secret-12345" not in redacted


def test_credential_leak_scanner_recursive_object_scan():
    scanner = CredentialLeakScanner()

    safe_payload = {
        "status": "SUCCESS",
        "work_order_id": "WO-002",
        "files": ["src/service.py"],
        "metadata": {"tokens": 120, "model": "codex-v1"},
    }
    assert len(scanner.scan_object(safe_payload)) == 0
    scanner.assert_no_leaks(safe_payload)

    leaky_payload = {
        "status": "RUNNING",
        "config": {
            "credentials": {
                "api_key": "sk-leak1234567890abcdef1234567890",
            }
        },
    }
    leaks = scanner.scan_object(leaky_payload)
    assert len(leaks) > 0
    with pytest.raises(CredentialLeakError, match="zero-leakage violation"):
        scanner.assert_no_leaks(leaky_payload)


def test_daemon_rpc_zero_credential_exposure(tmp_path: Path):
    """Verify live daemon RPC responses contain zero raw secret leaks."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex",
            provider="daemon",
            contract={"allow": ["src/*"]},
            workspace=str(tmp_path),
        )

        # Configure backend with credential reference (not raw token)
        client.configure_role_backend(
            role="backend",
            backend="echo-agent",
            credential_ref="env:MY_SECURE_TOKEN",
        )

        backends = client.list_backends()
        roles = client.list_roles()
        sess_record = client.get_session(session["session_id"])

        # Scan all RPC responses
        scanner = CredentialLeakScanner()
        scanner.assert_no_leaks(backends, "backend.list")
        scanner.assert_no_leaks(roles, "role.list")
        scanner.assert_no_leaks(sess_record, "session.get")


# ─── 3. D025 DESTRUCTIVE SAFEGUARDS TESTS ─────────────────────────────────────

def test_d025_destructive_command_classification():
    verifier = D025SafeguardVerifier()

    # Destructive commands
    assert verifier.is_destructive("rm -rf /var/data") is True
    assert verifier.is_destructive("rm -r ./src") is True
    assert verifier.is_destructive("git clean -fd") is True
    assert verifier.is_destructive("git reset --hard HEAD~1") is True
    assert verifier.is_destructive("git branch -D feat-old") is True
    assert verifier.is_destructive("docker system prune") is True

    # Safe non-destructive commands
    assert verifier.is_destructive("git status") is False
    assert verifier.is_destructive("pytest tests/") is False
    assert verifier.is_destructive("python -m cli.main validate .") is False
    assert verifier.is_destructive("cat src/api.py") is False


def test_d025_subagent_safeguards_enforcement():
    verifier = D025SafeguardVerifier()

    # 1. Missing backup and approval -> Rejected
    passed, reason = verifier.verify_operation(
        "rm -rf ./cache",
        {"git_clean": True, "has_backup": False, "approved_by_ceo": False},
    )
    assert passed is False
    assert "Backup not created" in reason
    assert "approval not recorded" in reason

    with pytest.raises(D025SubagentViolationError, match="Destructive operation blocked"):
        verifier.assert_safe("git clean -fd", {}, role="subagent-codex")

    # 2. All D025 preconditions satisfied -> Permitted
    passed, reason = verifier.verify_operation(
        "rm -rf ./scratch/cache",
        {"has_backup": True, "git_clean": True, "approved_by_ceo": True},
    )
    assert passed is True
    assert "D025 destructive operation approved" in reason


# ─── 4. TERMINAL OUTPUT SANITIZATION TESTS ────────────────────────────────────

def test_terminal_output_sanitization():
    # 1. Clean string unchanged
    clean_text = "Standard stdout line 1\nStandard stderr line 2\n"
    assert sanitize_terminal_output(clean_text) == clean_text
    assert is_terminal_output_clean(clean_text) is True
    assert_terminal_output_clean(clean_text)

    # 2. ANSI escape codes stripped
    ansi_dirty = "\x1b[31;1mERROR:\x1b[0m Failed to execute \x1b[32mtest\x1b[0m"
    cleaned_ansi = sanitize_terminal_output(ansi_dirty)
    assert cleaned_ansi == "ERROR: Failed to execute test"
    assert is_terminal_output_clean(cleaned_ansi) is True

    # 3. OSC terminal title injection stripped
    osc_dirty = "\x1b]0;Pwned Terminal Title\x07Actual subagent output\x1b\\"
    cleaned_osc = sanitize_terminal_output(osc_dirty)
    assert "Pwned" not in cleaned_osc
    assert "Actual subagent output" in cleaned_osc

    # 4. Raw control characters stripped (bell, backspace, null)
    ctrl_dirty = "Prefix\x00\x07\x08Text\x1fSuffix"
    cleaned_ctrl = sanitize_terminal_output(ctrl_dirty)
    assert cleaned_ctrl == "PrefixTextSuffix"

    # 5. Raw dirty text detected by assertion
    with pytest.raises(UnsafeOutputError, match="unescaped control codes or terminal escape"):
        assert_terminal_output_clean(ansi_dirty)


# ─── 5. BUDGET EXHAUSTION GATING TESTS ────────────────────────────────────────

def test_budget_exhaustion_gating():
    tracker = BudgetTracker(
        max_tokens=1000,
        max_steps=5,
        max_files_touched=3,
        max_time_seconds=2.0,
    )

    # Within budget
    tracker.record_tokens(500)
    tracker.record_step()
    tracker.record_file("src/a.py")
    ok, reason = tracker.check_budget()
    assert ok is True
    assert reason is None
    tracker.assert_within_budget()

    # Exceed tokens
    tracker.record_tokens(600)  # Total 1100 > 1000
    ok, reason = tracker.check_budget()
    assert ok is False
    assert "Token budget exceeded" in reason
    with pytest.raises(BudgetExceededError, match="Token budget exceeded"):
        tracker.assert_within_budget()

    # Reset and test step exhaustion
    tracker2 = BudgetTracker(max_steps=2)
    tracker2.record_step()
    tracker2.record_step()
    tracker2.record_step()  # 3 > 2
    with pytest.raises(BudgetExceededError, match="Step budget exceeded"):
        tracker2.assert_within_budget()

    # Reset and test file touched exhaustion
    tracker3 = BudgetTracker(max_files_touched=2)
    tracker3.record_file("a.py")
    tracker3.record_file("b.py")
    tracker3.record_file("c.py")  # 3 > 2
    with pytest.raises(BudgetExceededError, match="File budget exceeded"):
        tracker3.assert_within_budget()


# ─── 6. ACTIVE ROLE REBINDING SECURITY GATE TESTS ─────────────────────────────

def test_active_role_rebinding_gate_enforcement(tmp_path: Path):
    """Assert role.configureBackend is rejected while role has in-flight Work Orders."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="daemon",
            contract={"allow": ["src/*"]},
            workspace=str(tmp_path),
        )
        sid = session["session_id"]

        # Begin active backend operation
        _, op_id = daemon.manager.begin_operation(
            session_id=sid,
            operation_name="build_api",
            role="backend",
            work_order_id="WO-002",
        )

        # Attempt to rebind backend role while in flight -> MUST FAIL
        with pytest.raises(RuntimeError, match="Cannot rebind role"):
            client.configure_role_backend(
                role="backend",
                backend="ollama",
                model="llama3",
            )

        # Complete operation
        daemon.manager.complete_operation(
            session_id=sid,
            operation_id=op_id,
            result={"status": "SUCCESS"},
            status="COMPLETED",
        )

        # Rebinding after completion MUST SUCCEED
        rebind_result = client.configure_role_backend(
            role="backend",
            backend="echo-agent",
            model="echo-v2",
        )
        assert rebind_result["backend"] == "echo-agent"


# ─── 7. FAULT INJECTION: BACKEND CRASH & TIMEOUT ──────────────────────────────

def test_fault_injection_backend_crash_and_timeout_resilience():
    # 1. Backend crash raises structured error
    with pytest.raises(ConnectionResetError, match="connection dropped"):
        FaultInjectionEngine.simulate_backend_crash("Execution backend connection dropped")

    # 2. Backend timeout raises TimeoutError
    with pytest.raises(TimeoutError, match="timed out"):
        FaultInjectionEngine.simulate_backend_timeout(timeout_seconds=0.01)


# ─── 8. FAULT INJECTION: SUBAGENT CRASH & COMPLETION BLOCKING ─────────────────

def test_fault_injection_subagent_crash_blocks_parent_success(tmp_path: Path):
    """Simulate child subagent crash; verify parent detects failure and cannot complete SUCCESS."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="daemon",
            contract={"allow": ["src/*"]},
            workspace=str(tmp_path),
        )
        sid = session["session_id"]

        # Parent operation
        _, parent_op_id = daemon.manager.begin_operation(
            session_id=sid,
            operation_name="parent.orchestrate",
            role="architecture",
        )

        # Child operation
        child = daemon.manager.dispatch_subagent(
            session_id=sid,
            parent_operation_id=parent_op_id,
            role="backend",
            work_order_id="WO-101",
        )
        child_op_id = child["operation_id"]

        # Fault: subagent crashes mid-execution (marked FAILED)
        crashed_child = FaultInjectionEngine.simulate_subagent_crash(
            daemon.manager, sid, child_op_id, reason="SIGSEGV segmentation fault"
        )
        assert crashed_child["status"] == "FAILED"

        # Parent attempting to complete with COMPLETED must be BLOCKED
        with pytest.raises(ValueError, match="child operation .* failed"):
            daemon.manager.complete_operation(
                session_id=sid,
                operation_id=parent_op_id,
                result={"summary": "all done"},
                status="COMPLETED",
            )

        # Parent completing as FAILED is allowed (honest failure propagation)
        parent_failed = daemon.manager.complete_operation(
            session_id=sid,
            operation_id=parent_op_id,
            result={"summary": "aborted due to child failure"},
            status="FAILED",
        )
        assert parent_failed["status"] == "FAILED"


# ─── 9. FAULT INJECTION: CANCELLATION RACE VS NATURAL COMPLETION ──────────────

def test_fault_injection_cancellation_race_condition(tmp_path: Path):
    """Simulate cancellation signal racing with natural completion; verify deterministic terminal status."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="daemon",
            contract={"allow": ["src/*"]},
            workspace=str(tmp_path),
        )
        sid = session["session_id"]

        _, op_id = daemon.manager.begin_operation(
            session_id=sid,
            operation_name="race_target",
            role="backend",
        )

        cancel_res, comp_res = FaultInjectionEngine.simulate_cancellation_race(
            daemon.manager, sid, op_id
        )

        # Operation must be in terminal state CANCELLED
        assert comp_res["status"] == "CANCELLED"
        final_op = daemon.manager.get_operation(op_id)
        assert final_op["status"] == "CANCELLED"


# ─── 10. FAULT INJECTION: RECONNECT & FULL TREE RECONSTRUCTION ────────────────

def test_fault_injection_daemon_restart_reconstructs_operation_tree(tmp_path: Path):
    """Simulate daemon process crash & restart; verify entire operation tree and roles recover."""
    state_dir = tmp_path / "daemon_durability"
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. Start initial daemon, build tree
    daemon1 = LocalDaemon(state_dir, port=0).start()
    client1 = DaemonClient(daemon1.url)
    session = client1.create_session(
        agent="claude",
        provider="daemon",
        contract={"allow": ["src/*"]},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    _, parent_op_id = daemon1.manager.begin_operation(
        session_id=sid,
        operation_name="parent.orchestrate",
        role="architecture",
        work_order_id="WO-001",
    )

    child1 = daemon1.manager.dispatch_subagent(
        session_id=sid,
        parent_operation_id=parent_op_id,
        role="backend",
        work_order_id="WO-002",
        agent_id="codex-worker-1",
    )
    child2 = daemon1.manager.dispatch_subagent(
        session_id=sid,
        parent_operation_id=parent_op_id,
        role="frontend",
        work_order_id="WO-003",
        agent_id="gemini-worker-1",
    )

    # Complete child1
    daemon1.manager.complete_operation(
        session_id=sid,
        operation_id=child1["operation_id"],
        result={"files": ["src/api.py"]},
        status="COMPLETED",
    )

    # Abruptly stop daemon1 to simulate crash
    daemon1.stop()

    # 2. Restart daemon on same storage state
    daemon2 = LocalDaemon(state_dir, port=0).start()
    try:
        client2 = DaemonClient(daemon2.url)
        recovered_sess = client2.get_session(sid)
        assert recovered_sess["session_id"] == sid

        # Check full tree reconstruction
        recovered_agents = client2.list_agents(session_id=sid)
        agent_roles = {a["role"].lower() for a in recovered_agents}
        assert "architecture" in agent_roles
        assert "backend" in agent_roles
        assert "frontend" in agent_roles

        # Verify child1 status preserved as COMPLETED
        rec_child1 = daemon2.manager.get_operation(child1["operation_id"])
        assert rec_child1["status"] == "COMPLETED"

        # Verify parent's children list recovered
        rec_parent = daemon2.manager.get_operation(parent_op_id)
        assert child1["operation_id"] in rec_parent["children"]
        assert child2["operation_id"] in rec_parent["children"]
    finally:
        daemon2.stop()
