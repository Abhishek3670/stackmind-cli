"""Comprehensive test suite for Phase 0 Verification & Trust Foundation.

Covers:
1. Atomic write-lock TOCTOU protection & force-steal audit receipts.
2. Secret redaction & path-traversal containment in source excerpts.
3. Runner-owned WorkspaceSnapshot before/after diff derivation.
4. Observed vs. declared change comparison.
5. Contract enforcement on observed filesystem state.
6. Multi-dimensional verification and canonical trust eligibility gate.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest
import yaml

from cli.init import init
from cli.lock import acquire_lock, release_lock, read_lock, get_lock_path
from validators.harness.snapshot import (
    WorkspaceSnapshot,
    WorkspaceDiff,
    VerificationDimensions,
    TrustLevel,
    evaluate_learning_eligibility,
)
from validators.harness.contract_gate import verify_post_execution
from validators.knowledge.contract import AgentContract, ContractAccessDenied
from validators.knowledge.enricher import _source_excerpt, _redact_secrets


import shutil

@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "test-workspace"
    init(project, name="Test Workspace", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


# ─── 1. Write-Lock TOCTOU & Concurrency Protection ───────────────────────────

def test_atomic_acquire_lock_prevents_toctou(temp_workspace):
    sync_path = temp_workspace / ".sync"
    ok1, msg1 = acquire_lock(sync_path, "codex", session_id=1)
    assert ok1 is True
    assert "LOCK acquired by 'codex'" in msg1

    # Second acquisition by different agent without force must fail
    ok2, msg2 = acquire_lock(sync_path, "gemini", session_id=1, force=False)
    assert ok2 is False
    assert "LOCK held by 'codex'" in msg2

    # Re-acquisition by same agent succeeds
    ok3, msg3 = acquire_lock(sync_path, "codex", session_id=2)
    assert ok3 is True
    assert read_lock(sync_path)["session_id"] == 2


def test_force_acquire_creates_audit_receipt(temp_workspace):
    sync_path = temp_workspace / ".sync"
    acquire_lock(sync_path, "codex", session_id=10)

    # Force steal by gemini
    ok, msg = acquire_lock(sync_path, "gemini", session_id=11, force=True)
    assert ok is True
    assert "forcibly acquired" in msg.lower()
    assert read_lock(sync_path)["held_by"] == "gemini"

    # Verify audit receipt
    receipts = list((sync_path / "runtime" / "receipts").glob("LOCK_STOLEN_gemini_*.yaml"))
    assert len(receipts) == 1
    receipt_data = yaml.safe_load(receipts[0].read_text(encoding="utf-8"))
    assert receipt_data["event_type"] == "LOCK_STOLEN"
    assert receipt_data["stolen_from"] == "codex"
    assert receipt_data["agent"] == "gemini"


# ─── 2. Secret Redaction & Path-Safety ───────────────────────────────────────

def test_secret_redaction_patterns():
    raw_code = (
        'api_key = "secret_12345"\n'
        'bearer_token = "authorization: bearer eyJhbGciOi..."\n'
        'db_url = "postgres://user:pass123@localhost:5432/mydb"\n'
        'aws_key = "aws_secret_access_key = \'my-secret-key\'"\n'
    )
    redacted = _redact_secrets(raw_code)
    assert "secret_12345" not in redacted
    assert "pass123" not in redacted
    assert "my-secret-key" not in redacted
    assert "***" in redacted


def test_source_excerpt_path_traversal_blocked(temp_workspace):
    # Attempting to read outside workspace root via traversal
    outside_file = temp_workspace.parent / "outside_secret.py"
    outside_file.write_text("SUPER_SECRET_OUTSIDE = 42", encoding="utf-8")

    malicious_node = {
        "deterministic": {
            "path": "../outside_secret.py",
            "location": {"line": 1, "end_line": 2},
        }
    }
    excerpt = _source_excerpt(temp_workspace, malicious_node)
    assert excerpt == ""  # Path traversal must be blocked and return empty


# ─── 3. WorkspaceSnapshot & Diff Derivation ──────────────────────────────────

def test_workspace_snapshot_detects_all_changes(temp_workspace):
    file_a = temp_workspace / "module_a.py"
    file_a.write_text("def foo(): return 1\n", encoding="utf-8")

    file_b = temp_workspace / "module_b.py"
    file_b.write_text("def bar(): return 2\n", encoding="utf-8")

    # 1. Capture before snapshot
    before = WorkspaceSnapshot.capture(temp_workspace)
    assert "module_a.py" in before.files
    assert "module_b.py" in before.files

    # 2. Mutate workspace: modify A, delete B, add C
    file_a.write_text("def foo(): return 42 # modified\n", encoding="utf-8")
    file_b.unlink()
    file_c = temp_workspace / "module_c.py"
    file_c.write_text("def baz(): return 3 # added\n", encoding="utf-8")

    # 3. Capture after snapshot and derive diff
    after = WorkspaceSnapshot.capture(temp_workspace)
    diff = before.diff(after)

    assert diff.modified == ("module_a.py",)
    assert diff.deleted == ("module_b.py",)
    assert diff.added == ("module_c.py",)
    assert set(diff.all_changed_files) == {"module_a.py", "module_b.py", "module_c.py"}
    assert not diff.is_empty


def test_observed_vs_declared_comparison(temp_workspace):
    file_a = temp_workspace / "src" / "app.py"
    file_a.parent.mkdir(parents=True, exist_ok=True)
    file_a.write_text("app = 1", encoding="utf-8")

    before = WorkspaceSnapshot.capture(temp_workspace)
    file_a.write_text("app = 2", encoding="utf-8")
    after = WorkspaceSnapshot.capture(temp_workspace)
    diff = before.diff(after)

    # Clean match
    matches, reason = diff.matches_declaration(["src/app.py"])
    assert matches is True
    assert reason is None

    # Unannounced modification
    matches_fail, reason_fail = diff.matches_declaration([])
    assert matches_fail is False
    assert "Unannounced filesystem modifications" in reason_fail


# ─── 4. Contract Enforcement on Observed Filesystem State ────────────────────

def test_contract_enforces_on_observed_files(temp_workspace):
    # Setup contract denying 'auth' module
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-999",
        "scope": {
            "allow": [{"module": "app.*", "depth": 1}],
            "deny": [{"module": "auth.*"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 2},
    }
    contracts_dir = temp_workspace / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-999.yaml").write_text(yaml.dump(contract_data), encoding="utf-8")

    class MockTask:
        work_order_id = "WO-999"
        identifier = "task-1"

    class MockDecision:
        modified_files = ["app/main.py"]
        commands = ()

    # When LLM declares in-scope file but observed filesystem changed denied file
    with pytest.raises(ContractAccessDenied) as exc:
        verify_post_execution(
            temp_workspace,
            "codex",
            MockTask(),
            MockDecision(),
            observed_files=["auth/secret.py"],
        )
    assert "explicitly denied" in str(exc.value)


# ─── 5. Verification Dimensions & Trust Eligibility Gate ────────────────────

def test_learning_eligibility_trust_gate():
    # Fully passed dimensions + completed status + matching declaration -> LEARNING_ELIGIBLE
    dimensions_pass = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )
    trust = evaluate_learning_eligibility(
        decision_status="completed",
        dimensions=dimensions_pass,
        declaration_matches=True,
        has_unhandled_blockers=False,
    )
    assert trust == TrustLevel.LEARNING_ELIGIBLE

    # Mismatched declaration drops to VERIFIED (history only, not learning eligible)
    trust_mismatch = evaluate_learning_eligibility(
        decision_status="completed",
        dimensions=dimensions_pass,
        declaration_matches=False,
    )
    assert trust_mismatch == TrustLevel.VERIFIED

    # Incomplete verification drops to OBSERVABLE
    dimensions_fail = VerificationDimensions(
        scope_verified=True,
        state_verified=False,  # failed state validation
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=False,
    )
    trust_observable = evaluate_learning_eligibility(
        decision_status="completed",
        dimensions=dimensions_fail,
        declaration_matches=True,
    )
    assert trust_observable == TrustLevel.OBSERVABLE
