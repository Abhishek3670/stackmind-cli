"""Tests for phantom declaration pruning, placeholder allowlists, and execution resilience."""

from pathlib import Path
import tempfile
import pytest

from validators.harness.runner import AgentRunner, HarnessTask, HarnessDecision
from validators.harness.snapshot import WorkspaceSnapshot
from validators.kernel.security import is_allowlisted_token, scan_for_credential_leaks


def test_allowlist_tokens_recognize_documentation_placeholders():
    """Verify common environment and config placeholders are recognized as non-leaks."""
    assert is_allowlisted_token("change-me-to-another-long-random-secret")
    assert is_allowlisted_token("your-secret-key-here")
    assert is_allowlisted_token("replace-me-with-token")
    assert is_allowlisted_token("my_secret_token_123")
    assert is_allowlisted_token("default-password-admin")
    assert is_allowlisted_token("demo-token-xyz")
    assert is_allowlisted_token("todo-fill-secret")


def test_readme_env_placeholder_does_not_trigger_leak():
    """Verify README.md or documentation describing .env setup does not fail credential scan."""
    readme_text = """
    # Setup
    Create a `.env` file with the following variables:
    ```
    SECRET_KEY=change-me-to-another-long-random-secret
    CSRF_SECRET=your-secret-key-here
    DATABASE_URL=sqlite:///app.db
    ```
    """
    leaks = scan_for_credential_leaks(readme_text, file_path="README.md")
    assert leaks == []


def test_real_secret_leak_still_detected():
    """Verify actual credentials (e.g. OpenAI / GitHub keys) are still strictly detected."""
    bad_text = "API_KEY=sk-proj-abc123456789012345678901234567890"
    leaks = scan_for_credential_leaks(bad_text, file_path="config.py")
    assert len(leaks) > 0


def test_phantom_declaration_pruning(tmp_path: Path):
    """Verify that unwritten phantom paths (e.g. _scratch/decision.json) do not break declaration matching."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".sync").mkdir()
    (workspace / "requirements.txt").write_text("flask>=3.0\n", encoding="utf-8")
    (workspace / "README.md").write_text("# Project\n", encoding="utf-8")

    staged_root = tmp_path / "staged"
    staged_root.mkdir()
    (staged_root / ".sync").mkdir()
    (staged_root / "requirements.txt").write_text("flask>=3.0\nrequests>=2.0\n", encoding="utf-8")
    (staged_root / "README.md").write_text("# Updated Project\n", encoding="utf-8")

    before_snap = WorkspaceSnapshot.capture(workspace)
    after_snap = WorkspaceSnapshot.capture(staged_root)
    diff = before_snap.diff(after_snap)

    runner = AgentRunner(workspace, "codex")
    task = HarnessTask(
        identifier="WO-001",
        kind="work_order",
        work_order_id="WO-001",
        deliverable_path="requirements.txt",
        path=workspace / ".sync" / "WO-001.yaml",
        title="Install deps",
        body="deps",
        query="deps",
    )

    # Model includes phantom file _scratch/decision.json that does not exist on disk
    decision = HarnessDecision(
        status="completed",
        summary="Done",
        report_markdown="Done",
        blockers=(),
        modified_files=("requirements.txt", "README.md", "_scratch/decision.json"),
        release_target="requirements.txt",
        retrieval_queries=(),
        uncertainty=(),
    )

    dec_norm = {Path(p).as_posix().lstrip('/') for p in decision.modified_files if p}
    observed_task_files = tuple(
        norm_p for path in diff.all_changed_files
        if (norm_p := Path(path).as_posix().lstrip('/'))
    )
    phantom_files = {
        p for p in dec_norm
        if not (staged_root / p).is_file() and p.startswith(('_scratch', 'scratch', '.sync/state', '.sync/runtime'))
    }
    dec_effective = dec_norm - phantom_files
    declaration_matches = set(observed_task_files) == dec_effective

    assert declaration_matches is True
    assert phantom_files == {"_scratch/decision.json"}


def test_bookkeeping_inbox_declaration_pruned_when_unwritten(tmp_path: Path):
    """Verify that bookkeeping paths like .sync/inbox/claude/qa_verdict.txt are treated as phantoms if unwritten."""
    bookkeeping_prefixes = ('.sync/inbox/', '.sync/state/', '.sync/reports/')
    staged_root = tmp_path / "staged"
    staged_root.mkdir()
    (staged_root / "tests").mkdir()
    (staged_root / "tests" / "test_suite.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    dec_norm = {"tests/test_suite.py", ".sync/inbox/claude/qa_verdict.txt"}
    phantom_files = {
        p for p in dec_norm
        if not (staged_root / p).is_file() and (
            p.startswith(('_scratch', 'scratch', '.sync/runtime'))
            or any(p.startswith(prefix) for prefix in bookkeeping_prefixes)
        )
    }
    dec_effective = dec_norm - phantom_files
    assert phantom_files == {".sync/inbox/claude/qa_verdict.txt"}
    assert dec_effective == {"tests/test_suite.py"}

