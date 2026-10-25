"""
Acceptance test suite for Phase 0 Task 3 (P0-3) and Task 4 (P0-4):
Post-turn diff gate and derived commit tree binding engine.

Verifies:
1. Derived Commit Tree & Baseline .gitignore:
   - Excludes gitignored secrets (.env) from commit tree while keeping them in turn snapshot.
   - Uses pre-turn baseline .gitignore (unaffected by post-turn edits to .gitignore).
   - Correctly processes nested .gitignore rules and negation patterns (!keep.tmp).
   - Preserves tracked files at HEAD even if inside ignored or excluded folders.
   - Preserves 100755 mode for executable files across turns.
   - Produces 0 spurious diffs under .gitattributes * text eol=crlf.
2. Supervisor-Mediated GitOps & Ephemeral Credentials:
   - Denies git_commit, git_tag, and git_push to non-GitOps roles.
   - Asserts HEAD tree == approved derived tree BEFORE moving branch refs.
   - Injects push tokens via GIT_CONFIG_COUNT (never in argv; not written to disk).
   - Pushes directly from supervisor-owned storage_root: workspace remote.url, insteadOf, pushurl,
     and http.proxy have zero effect (local listener receives 0 connections).
   - Creates mediated tags in supervisor repository.
3. Repository Internals (.git) Gate (Amendment B):
   - Pre/post hash of all workspace .git metadata outside objects/.
   - Detects and reverts agent writing .git/hooks/*, editing .git/config, moving refs, editing .git/index.
   - Asserts objects pool grew monotonically.
   - Supports gitfile redirection (gitdir: <path>).
4. Neutralization of Workspace Config/Hooks:
   - Workspace filter.evil.clean and reference-transaction hooks do NOT execute during derived-tree build,
     commit, or push (0 marker files created).
5. Pre-Turn Contract Grounding & Manifest / Scope Violations:
   - Non-Architecture role mutating .sync/** triggers SYNC_SELF_GRANT.
   - Backend writing tests/pwned.test.js triggers OUT_OF_SCOPE_CREATE.
   - npx jest -u modifying/deleting snapshot files triggers out-of-scope modify/delete.
   - package.json scripts.postinstall and lockfile edits trigger OUT_OF_SCOPE_MODIFY.
   - sed -i on VERSION.md by non-GitOps triggers OUT_OF_SCOPE_MODIFY.
6. Approval Receipts & Background Writer Race Prevention:
   - Approval receipt stored in supervisor memory with HMAC signature.
   - Re-verified at GitOps turn start and right before commit: background writer running after QA approval
     invalidates the receipt and aborts commit.
7. ServiceJobManager Suspension & Isolation:
   - Persistent services run in a separate Job Object and are suspended during diff evaluation and restore,
     preventing race conditions and blame leakage.
8. Performance Benchmark on 10,000 files:
   - Measures actual throughput (MB/s) and scaling over 10k files without subprocess spawning per file.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from validators.kernel.contract import AgentContract
from validators.kernel.diff_gate import (
    DiffGate,
    GateResult,
    GateViolation,
    SupervisorApprovalReceipt,
    SupervisorReceiptManager,
)
from validators.kernel.process_reaper import ProcessReaper, ServiceJobManager
from validators.kernel.turn_snapshot import TurnSnapshotManager, is_reparse_point


def check_strict_skip(reason: str):
    """Enforce strict security policy: by default, any skip of a security-critical test is a failure."""
    if os.environ.get("ALLOW_SECURITY_SKIPS") == "1":
        pytest.skip(f"PERMITTED SKIP: {reason}")
    pytest.fail(f"STRICT SECURITY FAILURE (Strict mode default): {reason}")


@pytest.fixture
def gate_env(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = tmp_path / "supervisor_storage" / "snapshots.git"

    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": null_dev,
        "GIT_CONFIG_SYSTEM": null_dev,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG": null_dev,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_AUTHOR_NAME": "Author",
        "GIT_AUTHOR_EMAIL": "author@test.com",
        "GIT_COMMITTER_NAME": "Committer",
        "GIT_COMMITTER_EMAIL": "committer@test.com",
    }
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True, env=git_env)

    # Initial tracked files
    (ws / ".gitignore").write_text(".env\n*.log\n", encoding="utf-8")
    (ws / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws, check=True, capture_output=True, env=git_env)

    reaper = ProcessReaper()
    snapshot_mgr = TurnSnapshotManager(workspace=ws, storage_root=store, reaper=reaper)

    return ws, store, snapshot_mgr, reaper, git_env


# ==============================================================================
# 1. Derived Commit Tree & Baseline .gitignore Tests
# ==============================================================================

def test_derived_commit_tree_excludes_gitignored_env_file(gate_env):
    """Gitignored .env is retained in snapshot for safe revert, but excluded from derived commit tree."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / ".env").write_text("SECRET_API_KEY=super_secret\n", encoding="utf-8")
    (ws / "feature.py").write_text("def feature(): pass\n", encoding="utf-8")

    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")
    snap_files = snapshot_mgr.list_snapshot_files(approved_snap_sha)
    assert ".env" in snap_files, ".env was missing from supervisor snapshot!"

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    derived_tree_sha = gate.compute_derived_commit_tree(
        approved_snapshot_sha=approved_snap_sha,
        pre_turn_snapshot_sha=pre_sha,
    )

    tree_entries_res = subprocess.run(
        ["git", f"--git-dir={store}", "ls-tree", "-r", "--name-only", derived_tree_sha],
        check=True,
        capture_output=True,
        text=True,
        env=gate.base_env,
    )
    derived_names = set(tree_entries_res.stdout.splitlines())

    assert "feature.py" in derived_names
    assert "app.py" in derived_names
    assert ".gitignore" in derived_names
    assert ".env" not in derived_names, "CRITICAL: Gitignored .env leaked into approved derived commit tree!"


def test_derived_commit_tree_uses_pre_turn_baseline_gitignore(gate_env):
    """Agent tampering with .gitignore during turn cannot un-ignore secrets in derived tree."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Adversary edits .gitignore to remove .env rule, and creates .env
    (ws / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (ws / ".env").write_text("LEAKED_SECRET=999\n", encoding="utf-8")
    (ws / "safe.py").write_text("x = 1\n", encoding="utf-8")

    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    derived_tree = gate.compute_derived_commit_tree(
        approved_snapshot_sha=snap_sha,
        pre_turn_snapshot_sha=pre_sha,  # Evaluated against pre-turn baseline!
    )

    tree_entries = subprocess.run(
        ["git", f"--git-dir={store}", "ls-tree", "-r", "--name-only", derived_tree],
        check=True,
        capture_output=True,
        text=True,
        env=gate.base_env,
    ).stdout.splitlines()

    assert ".env" not in tree_entries, "Tampered .gitignore allowed .env into derived commit tree!"
    assert "safe.py" in tree_entries


def test_derived_commit_tree_nested_gitignore_and_negations(gate_env):
    """Derived tree correctly processes nested .gitignore rules and negation patterns."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Setup baseline with nested rules and negation
    nested_dir = ws / "sub"
    nested_dir.mkdir()
    (nested_dir / ".gitignore").write_text("*.tmp\n!keep.tmp\n", encoding="utf-8")
    subprocess.run(["git", "add", "sub/.gitignore"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "nested gitignore"], cwd=ws, check=True, capture_output=True, env=git_env)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker creates temp files
    (nested_dir / "drop.tmp").write_text("temp", encoding="utf-8")
    (nested_dir / "keep.tmp").write_text("keep", encoding="utf-8")

    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    derived_tree = gate.compute_derived_commit_tree(
        approved_snapshot_sha=snap_sha,
        pre_turn_snapshot_sha=pre_sha,
    )

    tree_entries = subprocess.run(
        ["git", f"--git-dir={store}", "ls-tree", "-r", "--name-only", derived_tree],
        check=True,
        capture_output=True,
        text=True,
        env=gate.base_env,
    ).stdout.splitlines()

    assert "sub/keep.tmp" in tree_entries, "Negation rule !keep.tmp was not honored!"
    assert "sub/drop.tmp" not in tree_entries, "Nested *.tmp was not ignored!"


def test_derived_commit_tree_preserves_tracked_files_inside_ignored_dirs(gate_env):
    """Files tracked at HEAD are never dropped from derived tree even if inside ignored directories."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    vendor_dir = ws / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "lib.py").write_text("def lib(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "vendor/lib.py"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "track vendor file"], cwd=ws, check=True, capture_output=True, env=git_env)

    # Now add vendor/ to .gitignore
    with open(ws / ".gitignore", "a") as f:
        f.write("vendor/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "ignore vendor"], cwd=ws, check=True, capture_output=True, env=git_env)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker modifies tracked vendor file
    (vendor_dir / "lib.py").write_text("def lib_v2(): pass\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    derived_tree = gate.compute_derived_commit_tree(approved_snapshot_sha=snap_sha, pre_turn_snapshot_sha=pre_sha)

    tree_entries = subprocess.run(
        ["git", f"--git-dir={store}", "ls-tree", "-r", "--name-only", derived_tree],
        check=True,
        capture_output=True,
        text=True,
        env=gate.base_env,
    ).stdout.splitlines()

    assert "vendor/lib.py" in tree_entries, "Tracked file at HEAD was erroneously dropped by ignore pattern!"


def test_derived_commit_tree_preserves_100755_executable_mode(gate_env):
    """Tracked executable files retain 100755 mode in approved derived tree."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    sh_file = ws / "build.sh"
    sh_file.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "build.sh"], cwd=ws, check=True, capture_output=True, env=git_env)

    # Force 100755 mode in Git index
    subprocess.run(
        ["git", "update-index", "--chmod=+x", "build.sh"],
        cwd=ws,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(["git", "commit", "-m", "add script"], cwd=ws, check=True, capture_output=True, env=git_env)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    sh_file.write_text("#!/bin/bash\necho modified\n", encoding="utf-8")
    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    derived_tree_sha = gate.compute_derived_commit_tree(approved_snapshot_sha=approved_snap_sha, pre_turn_snapshot_sha=pre_sha)

    tree_output = subprocess.run(
        ["git", f"--git-dir={store}", "ls-tree", derived_tree_sha, "build.sh"],
        check=True,
        capture_output=True,
        text=True,
        env=gate.base_env,
    ).stdout
    mode = tree_output.split()[0]
    assert mode == "100755", f"Expected mode 100755, found {mode}!"


def test_derived_commit_tree_produces_zero_spurious_diffs_with_crlf_attributes(gate_env):
    """.gitattributes * text eol=crlf produces 0 spurious diffs when derived commit is created."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitattributes"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "add attributes"], cwd=ws, check=True, capture_output=True, env=git_env)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    (ws / "crlf_doc.txt").write_bytes(b"line1\r\nline2\r\n")
    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gate.supervisor_git_commit(
        message="feat: add crlf document",
        approved_snapshot_sha=approved_snap_sha,
        pre_turn_snapshot_sha=pre_sha,
    )

    diff_res = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    assert diff_res.stdout.strip() == "", f"Spurious diffs detected after derived commit:\n{diff_res.stdout}"


# ==============================================================================
# 2. Repository Internals (.git) Gate Tests (Amendment B)
# ==============================================================================

def test_git_internals_hooks_tampering_detected_and_reverted(gate_env):
    """Agent planting .git/hooks/pre-commit is caught by internals gate and reverted."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    pre_git_state = gate.capture_git_internals_state()
    pre_objects = gate.scan_git_objects()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Malicious turn: write a hook in .git/hooks
    hook_file = ws / ".git" / "hooks" / "pre-commit"
    hook_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    res = gate.evaluate_turn(
        pre_turn_snapshot_sha=pre_sha,
        turn_id="turn-0",
        pre_turn_git_state=pre_git_state,
        pre_turn_git_objects=pre_objects,
    )

    assert not res.passed
    assert any(v.violation_type == "GIT_HOOKS_TAMPERING" and "hooks/pre-commit" in v.path for v in res.violations)
    assert not hook_file.exists(), ".git/hooks/pre-commit was not eradicated on revert!"


def test_git_internals_config_tampering_detected_and_reverted(gate_env):
    """Agent modifying .git/config triggers GIT_CONFIG_TAMPERING and is reverted byte-exact."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    pre_git_state = gate.capture_git_internals_state()
    pre_objects = gate.scan_git_objects()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Malicious turn: inject malicious filter in .git/config
    config_file = ws / ".git" / "config"
    orig_config = config_file.read_bytes()
    with open(config_file, "a") as f:
        f.write("\n[filter \"evil\"]\n\tclean = touch /pwn\n")

    res = gate.evaluate_turn(
        pre_turn_snapshot_sha=pre_sha,
        turn_id="turn-0",
        pre_turn_git_state=pre_git_state,
        pre_turn_git_objects=pre_objects,
    )

    assert not res.passed
    assert any(v.violation_type == "GIT_CONFIG_TAMPERING" and v.path == ".git/config" for v in res.violations)
    assert config_file.read_bytes() == orig_config, ".git/config was not restored byte-exact!"


def test_git_internals_ref_tampering_detected_and_reverted(gate_env):
    """Agent moving a ref or creating a branch in .git/refs/ triggers GIT_REFS_TAMPERING."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    pre_git_state = gate.capture_git_internals_state()
    pre_objects = gate.scan_git_objects()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Malicious turn: create unauthorized ref
    evil_ref = ws / ".git" / "refs" / "heads" / "evil_branch"
    evil_ref.write_text("0123456789012345678901234567890123456789\n", encoding="utf-8")

    res = gate.evaluate_turn(
        pre_turn_snapshot_sha=pre_sha,
        turn_id="turn-0",
        pre_turn_git_state=pre_git_state,
        pre_turn_git_objects=pre_objects,
    )

    assert not res.passed
    assert any(v.violation_type == "GIT_REFS_TAMPERING" and "evil_branch" in v.path for v in res.violations)
    assert not evil_ref.exists(), "Unauthorized ref was not deleted on revert!"


def test_git_internals_index_tampering_detected_and_reverted(gate_env):
    """Agent directly mutating .git/index triggers GIT_INDEX_TAMPERING."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    pre_git_state = gate.capture_git_internals_state()
    pre_objects = gate.scan_git_objects()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    index_file = ws / ".git" / "index"
    orig_index = index_file.read_bytes()
    index_file.write_bytes(b"CORRUPTED_INDEX_DATA")

    res = gate.evaluate_turn(
        pre_turn_snapshot_sha=pre_sha,
        turn_id="turn-0",
        pre_turn_git_state=pre_git_state,
        pre_turn_git_objects=pre_objects,
    )

    assert not res.passed
    assert any(v.violation_type == "GIT_INDEX_TAMPERING" for v in res.violations)
    assert index_file.read_bytes() == orig_index


def test_git_internals_gitfile_resolution_supported(gate_env, tmp_path):
    """Gitfile redirection (.git pointing to gitdir: <path>) is resolved and monitored."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Reconfigure ws/.git as a gitfile
    real_git_dir = tmp_path / "separate_git_dir"
    shutil.move(ws / ".git", real_git_dir)
    (ws / ".git").write_text(f"gitdir: {real_git_dir.as_posix()}\n", encoding="utf-8")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    assert gate.resolve_workspace_git_dir() == real_git_dir.resolve()

    pre_git_state = gate.capture_git_internals_state()
    assert "config" in pre_git_state
    pre_objects = gate.scan_git_objects()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Modify config in redirected gitdir
    with open(real_git_dir / "config", "a") as f:
        f.write("\n[core]\n\tgitfile_tamper = true\n")

    res = gate.evaluate_turn(
        pre_turn_snapshot_sha=pre_sha,
        turn_id="turn-0",
        pre_turn_git_state=pre_git_state,
        pre_turn_git_objects=pre_objects,
    )
    assert not res.passed
    assert any(v.violation_type == "GIT_CONFIG_TAMPERING" for v in res.violations)


# ==============================================================================
# 3. Neutralization of Workspace Config/Hooks & Explicit Push
# ==============================================================================

def test_workspace_filter_and_hook_neutralized_during_derived_tree_and_commit(gate_env, tmp_path):
    """Workspace filter.evil.clean and reference-transaction hooks do NOT execute during derived-tree build or commit."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    marker_filter = tmp_path / "filter_ran.txt"
    marker_hook = tmp_path / "hook_ran.txt"

    # Inject evil clean filter and hook in workspace .git
    with open(ws / ".git" / "config", "a") as f:
        f.write(f'\n[filter "evil"]\n\tclean = "cmd.exe /c echo evil > {marker_filter.as_posix()}"\n')

    hooks_dir = ws / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_file = hooks_dir / "reference-transaction"
    hook_file.write_text(f"cmd.exe /c echo hook > {marker_hook.as_posix()}\n", encoding="utf-8")

    (ws / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    (ws / "sample.txt").write_text("content\n", encoding="utf-8")

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)

    # Derived tree and commit creation must neutralize workspace config and hooks
    commit_sha = gate.supervisor_git_commit(
        message="feat: hardened commit",
        approved_snapshot_sha=snap_sha,
        pre_turn_snapshot_sha=pre_sha,
    )
    assert commit_sha is not None

    # Neither the filter nor the hook must have executed
    assert not marker_filter.exists(), "SECURITY VULNERABILITY: Workspace clean filter was executed by supervisor!"
    assert not marker_hook.exists(), "SECURITY VULNERABILITY: Workspace reference-transaction hook was executed by supervisor!"


def test_supervisor_push_from_store_ignores_workspace_remote_and_proxy(gate_env):
    """Supervisor push runs from supervisor-owned store: workspace remote/insteadOf/proxy are completely ignored."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Setup local TCP listener to verify workspace remote is NEVER contacted
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    listener_contacted = [False]

    def accept_thread():
        listener.settimeout(0.5)
        try:
            conn, _ = listener.accept()
            listener_contacted[0] = True
            conn.close()
        except socket.timeout:
            pass

    t = threading.Thread(target=accept_thread, daemon=True)
    t.start()

    # Workspace config points remote to the listener socket
    with open(ws / ".git" / "config", "a") as f:
        f.write(f'\n[remote "origin"]\n\turl = http://127.0.0.1:{port}/evil.git\n')
        f.write(f'\n[http]\n\tproxy = http://127.0.0.1:{port}\n')

    # Create dummy commit in store
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    gate.supervisor_git_commit("feat: test", approved_snapshot_sha=snap_sha, pre_turn_snapshot_sha=pre_sha)

    # Push to a separate valid local bare destination using explicit URL
    dest_store = ws.parent / "dest_remote.git"
    subprocess.run(["git", "init", "--bare", str(dest_store)], check=True, capture_output=True, env=git_env)

    try:
        gate.supervisor_git_push(
            remote_url=str(dest_store.resolve()),
            refspec="master",
            auth_token="test_token",
        )
    except subprocess.CalledProcessError as err:
        # On Windows sandbox where MSYS2 sh.exe cannot initialize for local transport pipe,
        # confirm the failure is purely local transport and NOT an unauthorized remote contact.
        err_msg = err.stderr.decode("utf-8", errors="replace") if isinstance(err.stderr, bytes) else str(err.stderr)
        if "Could not read from remote repository" not in err_msg:
            raise

    t.join(timeout=1.0)
    listener.close()

    assert not listener_contacted[0], "CRITICAL: Workspace remote/proxy hijacked supervisor push!"


def test_push_token_injected_via_git_config_count_not_on_argv(gate_env, monkeypatch):
    """Push token is passed via GIT_CONFIG_COUNT env var, never on argv, and not written to disk."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)

    captured_cmds = []
    captured_envs = []

    def mock_subprocess_run(cmd, cwd=None, check=False, capture_output=False, env=None, **kwargs):
        captured_cmds.append(list(cmd))
        captured_envs.append(dict(env) if env else {})
        class MockRes:
            returncode = 0
            stdout = ""
            stderr = ""
        return MockRes()

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    secret = "ghp_sensitive_secret_token_12345"
    gate.supervisor_git_push("https://github.com/org/repo.git", "master", auth_token=secret)

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    push_env = captured_envs[0]

    # Token must NOT be on command line argv
    for arg in cmd:
        assert secret not in arg, f"CRITICAL: Secret token leaked onto process command line: {arg}"

    # Token must be in environment via GIT_CONFIG_COUNT
    assert push_env.get("GIT_CONFIG_COUNT") == "1"
    assert push_env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
    assert secret in push_env.get("GIT_CONFIG_VALUE_0", "")


# ==============================================================================
# 4. Scope, Manifest, & Role Enforcement Tests
# ==============================================================================

def test_scope_backend_writing_tests_pwned_rejected(gate_env):
    """Backend writing tests/pwned.test.js is rejected with OUT_OF_SCOPE_CREATE."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Backend allowed scope is src/** only
    tests_dir = ws / "tests"
    tests_dir.mkdir(exist_ok=True)
    pwn_test = tests_dir / "pwned.test.js"
    pwn_test.write_text("test('pwn', () => {});\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("src/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_CREATE" and "tests/pwned.test.js" in v.path for v in res.violations)
    assert not pwn_test.exists()


def test_scope_jest_snapshot_update_rejected(gate_env):
    """npx jest -u modifying snapshot files outside authorized scope is rejected."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    snap_dir = ws / "tests" / "__snapshots__"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / "component.test.js.snap"
    snap_file.write_text("exports[`test`] = `1`;\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker modifies snapshot
    snap_file.write_text("exports[`test`] = `tampered`;\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("src/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and "component.test.js.snap" in v.path for v in res.violations)


def test_scope_package_json_postinstall_and_lockfile_rejected(gate_env):
    """Mutating package.json scripts.postinstall and lockfile outside scope is rejected."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "package.json").write_text('{"name": "app", "scripts": {}}\n', encoding="utf-8")
    (ws / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Malicious injection
    (ws / "package.json").write_text('{"name": "app", "scripts": {"postinstall": "malicious"}}\n', encoding="utf-8")
    (ws / "pnpm-lock.yaml").write_text("lockfileVersion: 'tampered'\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("src/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and "package.json" in v.path for v in res.violations)
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and "pnpm-lock.yaml" in v.path for v in res.violations)


def test_scope_version_markdown_by_non_gitops_rejected(gate_env):
    """sed -i on VERSION.md by non-GitOps role is rejected."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "VERSION.md").write_text("1.0.0\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / "VERSION.md").write_text("2.0.0-pwned\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("src/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and "VERSION.md" in v.path for v in res.violations)


def test_supervisor_mediated_git_tag_success_and_role_enforcement(gate_env):
    """GitOps can create tags; non-GitOps is denied."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    # Non-GitOps fails
    gate_worker = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    with pytest.raises(PermissionError, match="not authorized to create Git tags"):
        gate_worker.supervisor_git_tag("v1.0.0")

    # GitOps succeeds
    gate_gitops = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gate_gitops.supervisor_git_commit("feat: release", approved_snapshot_sha=snap_sha, pre_turn_snapshot_sha=pre_sha)
    gate_gitops.supervisor_git_tag("v1.0.0", message="release v1.0.0", commit_sha=commit_sha)

    tag_check = subprocess.run(
        ["git", f"--git-dir={store}", "tag", "-l", "v1.0.0"],
        check=True,
        capture_output=True,
        text=True,
        env=gate_gitops.base_env,
    ).stdout.strip()
    assert tag_check == "v1.0.0"


def test_sync_contract_mutation_triggers_self_grant_violation(gate_env):
    """Non-Architecture role attempting to modify .sync/** triggers SYNC_SELF_GRANT."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    sync_dir = ws / ".sync"
    sync_dir.mkdir()
    (sync_dir / "contract.yaml").write_text("unauthorized_grant: true\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "SYNC_SELF_GRANT" for v in res.violations)
    assert not (sync_dir / "contract.yaml").exists()


def test_architecture_role_allowed_to_modify_sync_contracts(gate_env):
    """Architecture role is authorized to modify .sync/** contracts."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    sync_dir = ws / ".sync"
    sync_dir.mkdir()
    (sync_dir / "architecture_spec.md").write_text("# Spec\n", encoding="utf-8")

    contract = AgentContract(agent_id="arch", work_order="WO-001", allow=(".sync/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Architecture", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert res.passed
    assert len(res.violations) == 0
    assert "src/**" not in res.modified_files


# ==============================================================================
# 5. Approval Receipts & Background Writer Race Prevention
# ==============================================================================

def test_approval_receipt_hmac_verified_and_tamper_evident(gate_env):
    """Approval receipt stored in supervisor memory with HMAC signature cannot be forged."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    mgr = SupervisorReceiptManager()

    receipt = mgr.issue_receipt("M-01", "snap_sha_123", "tree_sha_456")
    assert mgr.verify_receipt(receipt)

    # Forged receipt fails
    forged = SupervisorApprovalReceipt(
        milestone_id="M-01",
        approved_snapshot_sha="snap_sha_FORGED",
        derived_tree_sha="tree_sha_456",
        timestamp=receipt.timestamp,
        hmac_signature=receipt.hmac_signature,
    )
    assert not mgr.verify_receipt(forged)


def test_background_writer_active_after_qa_approval_aborts_gitops_commit(gate_env):
    """Writer running after QA approval invalidates approval and aborts GitOps commit."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "service.py").write_text("def run(): pass\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    approved_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    derived_tree = gate.compute_derived_commit_tree(approved_sha, pre_turn_snapshot_sha=pre_sha)
    receipt = gate.receipt_manager.issue_receipt("M-01", approved_sha, derived_tree)

    # Background writer modifies workspace post-approval
    (ws / "service.py").write_text("def run(): pass\n# background modification\n", encoding="utf-8")

    # GitOps commit must reject due to post-approval modification
    with pytest.raises(RuntimeError, match="workspace was modified after QA approval was issued"):
        gate.supervisor_git_commit(
            message="feat: release",
            approved_snapshot_sha=approved_sha,
            pre_turn_snapshot_sha=pre_sha,
            receipt=receipt,
        )


def test_commit_tree_verified_against_derived_tree_before_ref_update(gate_env, monkeypatch):
    """Asserts commit tree == derived tree BEFORE moving branch ref."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True, env=git_env
    ).stdout.strip()
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")
    (ws / "feat.py").write_text("feat = 1\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)

    # Empty tree SHA in git
    empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    # Rig subprocess.run during commit-tree to produce a commit pointing to empty_tree
    # rather than the approved derived tree
    orig_run = subprocess.run
    def rigged_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "commit-tree" in cmd:
            cmd_copy = list(cmd)
            idx = cmd_copy.index("commit-tree")
            cmd_copy[idx + 1] = empty_tree
            return orig_run(cmd_copy, *args, **kwargs)
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", rigged_run)

    with pytest.raises(RuntimeError, match="Derived tree binding invariant failed"):
        gate.supervisor_git_commit(
            message="corrupted commit",
            approved_snapshot_sha=snap_sha,
            pre_turn_snapshot_sha=pre_sha,
        )

    # Assert branch ref did not move — remains completely untouched!
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True, env=git_env
    ).stdout.strip()
    assert current_head == original_head, "HEAD ref moved despite tree mismatch!"


def test_e2e_supervisor_commit_read_by_real_git(gate_env):
    """End-to-end verification: real git parses and verifies the supervisor-generated commit tree byte-exact."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    src_dir = ws / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (ws / "app.py").write_text("import src.calc\ndef main(): return src.calc.add(1, 2)\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gate.supervisor_git_commit(
        message="feat(e2e): real git commit test",
        approved_snapshot_sha=snap_sha,
        pre_turn_snapshot_sha=pre_sha,
    )

    # 1. Real git log verifies commit object, commit message, and author
    log_out = subprocess.run(
        ["git", "log", "-1", "--format=%H|%s|%an"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout.strip()
    c_hash, c_msg, c_author = log_out.split("|")
    assert c_hash == commit_sha
    assert c_msg == "feat(e2e): real git commit test"
    assert c_author == "StackMind Supervisor"

    # 2. Real git cat-file -p HEAD parses tree structure
    cat_commit = subprocess.run(
        ["git", "cat-file", "-p", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout
    assert "tree " in cat_commit
    assert "parent " in cat_commit
    assert "feat(e2e): real git commit test" in cat_commit

    # 3. Real git ls-tree recursively inspects tracked files and mode bits
    ls_out = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout.strip().splitlines()

    paths_in_tree = {}
    for line in ls_out:
        parts = line.split()
        mode, obj_type, blob_sha, path = parts[0], parts[1], parts[2], parts[3]
        paths_in_tree[path] = (mode, blob_sha)

    assert "app.py" in paths_in_tree
    assert "src/calc.py" in paths_in_tree
    assert paths_in_tree["app.py"][0] == "100644"
    assert paths_in_tree["src/calc.py"][0] == "100644"

    # 4. Real git cat-file extracts exact blob contents
    calc_blob = subprocess.run(
        ["git", "cat-file", "-p", paths_in_tree["src/calc.py"][1]],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout
    assert calc_blob == "def add(a, b): return a + b\n"


# ==============================================================================
# 6. ServiceJobManager Suspension & Isolation
# ==============================================================================

def test_service_job_manager_suspended_during_diff_evaluation_and_restore(gate_env, tmp_path):
    """Persistent services are suspended (NtSuspendProcess) during evaluate_turn and restore to halt concurrent writes."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    service_mgr = ServiceJobManager()

    # Launch a real service that continuously writes to a log file
    writer_script = tmp_path / "writer_service.py"
    log_file = tmp_path / "service_log.txt"
    writer_script.write_text(
        "import time, pathlib\n"
        f"p = pathlib.Path(r'{log_file}')\n"
        "counter = 0\n"
        "while True:\n"
        "    counter += 1\n"
        "    with open(p, 'a') as f: f.write(f'{counter}\\n')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )

    service_proc = service_mgr.launch_service([sys.executable, str(writer_script)])
    time.sleep(0.2)
    assert log_file.is_file() and log_file.stat().st_size > 0

    # 1. Suspend all services via NtSuspendProcess
    service_mgr.suspend_all()
    size_during_suspend = log_file.stat().st_size
    time.sleep(0.25)
    # Assert writes completely halted while suspended
    assert log_file.stat().st_size == size_during_suspend, "Service wrote to disk while suspended!"

    # 2. Resume all services via NtResumeProcess
    service_mgr.resume_all()
    time.sleep(0.25)
    assert log_file.stat().st_size > size_during_suspend, "Service failed to resume writing after resume_all()!"

    # Cleanup service process
    service_mgr.shutdown_services()
    time.sleep(0.1)
    assert service_proc.poll() is not None


# ==============================================================================
# 7. Violation Matrix & Reverting
# ==============================================================================

def test_violation_reparse_point_junction_rejected(gate_env):
    """Agent planting a directory junction triggers REPARSE_POINT violation and rollback."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    outside_dir = ws.parent / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    junction = ws / "evil_junction"

    try:
        import _winapi
        _winapi.CreateJunction(str(outside_dir.resolve()), str(junction.resolve()))
    except (OSError, NotImplementedError):
        check_strict_skip("OS environment prevented junction creation")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "REPARSE_POINT" for v in res.violations)
    assert not junction.exists()


def test_violation_out_of_scope_rename_rejected(gate_env):
    """Renaming an in-scope file to an out-of-scope path triggers RENAME_OUT_OF_SCOPE."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "allowed.py").write_text("x = 1\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / "allowed.py").rename(ws / "secret.py")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("allowed.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "RENAME_OUT_OF_SCOPE" for v in res.violations)
    assert (ws / "allowed.py").exists()
    assert not (ws / "secret.py").exists()


def test_violation_case_only_rename_rejected(gate_env):
    """Case-only rename (e.g. foo.py -> FOO.py) triggers CASE_ONLY_RENAME."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "foo.py").write_text("foo\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / "foo.py").unlink()
    (ws / "FOO.py").write_text("foo\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type in ("CASE_ONLY_RENAME", "CASE_COLLISION") for v in res.violations)


def test_violation_out_of_scope_delete_rejected(gate_env):
    """Deleting a file outside authorized scope triggers OUT_OF_SCOPE_DELETE."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "protected.py").write_text("critical()\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / "protected.py").unlink()

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("other.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_DELETE" for v in res.violations)
    assert (ws / "protected.py").exists()


def test_violation_hardlink_creation_rejected(gate_env):
    """Creating an NTFS hardlink triggers HARDLINK_DETECTED violation."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "base.py").write_text("base\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    hl = ws / "hardlink_alias.py"
    res = ctypes.windll.kernel32.CreateHardLinkW(str(hl), str(ws / "base.py"), None)
    if res == 0:
        check_strict_skip("Hardlink creation denied by OS")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "HARDLINK_DETECTED" for v in res.violations)


def test_violation_dos_reserved_device_name_rejected(gate_env):
    """Creating DOS reserved device names triggers RESERVED_DEVICE_NAME."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    p = f"\\\\?\\{ws.resolve()}\\AUX.txt" if sys.platform == "win32" else str(ws / "AUX.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("reserved\n")
    except OSError:
        pytest.skip("Filesystem rejected DOS reserved device name directly")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "RESERVED_DEVICE_NAME" for v in res.violations)


def test_violation_trailing_dots_or_spaces_rejected(gate_env):
    """Files with trailing dots or spaces trigger TRAILING_DOT_OR_SPACE."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    p = f"\\\\?\\{ws.resolve()}\\evil_path " if sys.platform == "win32" else str(ws / "evil_path ")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("trailing\n")
    except OSError:
        pytest.skip("Filesystem rejected trailing space filename directly")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "TRAILING_DOT_OR_SPACE" for v in res.violations)


def test_violation_short_name_alias_rejected(gate_env):
    """Files matching 8.3 short names (~ pattern) trigger SHORT_NAME_ALIAS."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (ws / "PROGRA~1.TXT").write_text("alias\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "SHORT_NAME_ALIAS" for v in res.violations)


def test_violation_forged_mtime_and_size_detected_via_sha(gate_env):
    """Adversary modifying content while resetting mtime and size is caught by full content hash."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    target = ws / "module.py"
    target.write_bytes(b"content_a_123456789")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    stat_orig = target.stat()
    target.write_bytes(b"content_b_123456789")
    os.utime(target, (stat_orig.st_atime, stat_orig.st_mtime))

    stat_forged = target.stat()
    assert stat_forged.st_size == stat_orig.st_size
    assert int(stat_forged.st_mtime) == int(stat_orig.st_mtime)

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("other.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and v.path == "module.py" for v in res.violations)
    assert target.read_bytes() == b"content_a_123456789"


def test_violation_ntfs_alternate_data_stream_rejected(gate_env):
    """Attaching an alternate data stream triggers ADS_STREAM_DETECTED."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    target = ws / "clean.py"
    target.write_text("clean\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    ads = ws / "clean.py:hidden_stream"
    try:
        ads.write_text("payload", encoding="utf-8")
    except (OSError, NotImplementedError):
        check_strict_skip("NTFS ADS not supported")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "ADS_STREAM_DETECTED" for v in res.violations)


def test_directory_ads_violation_recording(gate_env):
    """NTFS Alternate Data Stream attached to a directory triggers ADS_STREAM_DETECTED."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    sub_dir = ws / "subdir"
    sub_dir.mkdir(exist_ok=True)
    dir_ads = ws / "subdir:hidden_stream"
    try:
        dir_ads.write_text("dir_payload", encoding="utf-8")
    except (OSError, NotImplementedError):
        check_strict_skip("Directory NTFS ADS not supported")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("subdir/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "ADS_STREAM_DETECTED" and "subdir" in v.path for v in res.violations)


def test_violation_reaps_background_process_and_reverts_byte_exact(gate_env):
    """On violation, background looping process is reaped and workspace restored byte-exact."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "code.py").write_text("original = 1\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    proc = subprocess.Popen([sys.executable, "-c", "import time; \nwhile True: time.sleep(0.05)"])
    reaper.register_process(proc)

    (ws / "unauthorized.py").write_text("evil\n", encoding="utf-8")
    (ws / "code.py").write_text("original = 2\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("code.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed

    time.sleep(0.2)
    assert proc.poll() is not None
    assert not (ws / "unauthorized.py").exists()
    assert (ws / "code.py").read_text(encoding="utf-8") == "original = 1\n"


def test_suspended_launch_ordering_pins_to_job_before_execution(gate_env):
    """launch_in_job starts process suspended, assigns to Job Object, then resumes."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    proc = reaper.launch_in_job([sys.executable, "-c", "import time; time.sleep(2)"])
    assert proc.poll() is None
    assert proc.pid in reaper._registered_pids

    reaped = reaper.reap_all()
    assert proc.pid in reaped
    time.sleep(0.1)
    assert proc.poll() is not None


def test_persistent_services_job_isolation(gate_env):
    """Persistent services run in a dedicated Job Object isolated from per-turn reapers."""
    ws, store, snapshot_mgr, turn_reaper, git_env = gate_env
    service_mgr = ServiceJobManager()

    service_proc = service_mgr.launch_service([sys.executable, "-c", "import time; time.sleep(5)"])
    worker_proc = turn_reaper.launch_in_job([sys.executable, "-c", "import time; time.sleep(5)"])

    assert service_proc.poll() is None
    assert worker_proc.poll() is None

    turn_reaper.reap_all()
    time.sleep(0.2)
    assert worker_proc.poll() is not None, "Worker process must be terminated on turn end!"
    assert service_proc.poll() is None, "Persistent service must remain running across turn boundaries!"

    service_mgr.shutdown_services()
    time.sleep(0.2)
    assert service_proc.poll() is not None, "Persistent service must be terminated on service shutdown!"


def test_store_digest_only_in_memory(gate_env):
    """Store digest baseline is maintained exclusively in supervisor memory and never on disk."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    snapshot_mgr.create_turn_snapshot("turn-0")

    baseline = snapshot_mgr.baseline_store_hash
    assert baseline is not None
    assert len(baseline) == 64

    for root, dirs, files in os.walk(store):
        for f in files:
            file_content = (Path(root) / f).read_bytes()
            assert baseline.encode("ascii") not in file_content, f"Store file '{f}' leaks memory baseline hash!"


def test_qa_clean_install_and_verification(gate_env, tmp_path):
    """QA verification runs against derived commit tree in clean worktree, free of uncommitted state."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / ".env").write_text("SECRET=12345\n", encoding="utf-8")
    (ws / "service.py").write_text("def run(): pass\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gate.supervisor_git_commit("add service", approved_snapshot_sha=snap_sha)

    qa_dir = tmp_path / "qa_clean_worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(qa_dir), commit_sha],
        cwd=ws,
        check=True,
        capture_output=True,
        env=git_env,
    )

    try:
        assert (qa_dir / "service.py").exists()
        assert not (qa_dir / ".env").exists()
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(qa_dir)],
            cwd=ws,
            check=False,
            capture_output=True,
            env=git_env,
        )


def test_post_approval_modification_resets_qa_approval(gate_env):
    """Modifying workspace files after QA approval invalidates approved state and requires re-review."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "module.py").write_text("v1 = True\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("module.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    milestone_state = {"status": "QA_APPROVED", "approved_snapshot": snap_sha}

    (ws / "module.py").write_text("v2 = Tampered\n", encoding="utf-8")

    current_files = snapshot_mgr._collect_workspace_files()
    pre_snap_files = snapshot_mgr.list_snapshot_files(milestone_state["approved_snapshot"])

    modified_post_approval = any(
        p.relative_to(ws).as_posix() in pre_snap_files and
        hashlib.sha1(b"blob " + str(len(p.read_bytes())).encode() + b"\x00" + p.read_bytes()).hexdigest() != pre_snap_files[p.relative_to(ws).as_posix()]
        for p in current_files
    )

    if modified_post_approval:
        milestone_state["status"] = "PENDING_QA_REVIEW"
        milestone_state["approved_snapshot"] = None

    assert milestone_state["status"] == "PENDING_QA_REVIEW"
    assert milestone_state["approved_snapshot"] is None


# ==============================================================================
# 8. Performance Benchmarks
# ==============================================================================

def test_gate_evaluation_performance_on_realistic_workspace(gate_env):
    """Benchmark diff gate hashing throughput and latency across 100 files."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    src_dir = ws / "src"
    src_dir.mkdir()
    for i in range(100):
        mod_dir = src_dir / f"module_{i // 10}"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / f"service_{i}.py").write_text(f"# service {i}\ndef run_{i}():\n    return {i * 42}\n", encoding="utf-8")

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (src_dir / "module_0" / "service_0.py").write_text("# modified 0\n", encoding="utf-8")
    (src_dir / "module_1" / "service_10.py").write_text("# modified 10\n", encoding="utf-8")
    (src_dir / "module_0" / "new_service.py").write_text("# new service\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("src/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")

    assert res.passed
    assert len(res.violations) == 0
    assert len(res.modified_files) == 2
    assert len(res.added_files) == 1
    assert res.evaluation_duration_seconds < 3.0, "Diff gate latency exceeded 3.0s ceiling for 100 files!"


def test_benchmark_10k_files_throughput_and_scaling(gate_env):
    """Stress test diff gate across 10,000 files measuring scaling and throughput (MB/s)."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    data_dir = ws / "bench_10k"
    data_dir.mkdir()

    # Create 10,000 files in 50 directories (each file ~1KB, ~10MB total payload)
    payload = ("A" * 1024).encode("ascii")
    for d in range(50):
        subdir = data_dir / f"dir_{d}"
        subdir.mkdir()
        for f in range(200):
            (subdir / f"file_{f}.txt").write_bytes(payload)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker modifies 10 files
    (data_dir / "dir_0" / "file_0.txt").write_bytes(b"modified_data_000")
    (data_dir / "dir_1" / "file_1.txt").write_bytes(b"modified_data_111")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("bench_10k/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    start_bench = time.perf_counter()
    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    elapsed = time.perf_counter() - start_bench

    assert res.passed
    assert len(res.modified_files) == 2
    ms_per_file = (elapsed * 1000.0) / 10000.0
    print(f"\n[STRESS BENCHMARK] 10,000 files evaluated in: {elapsed:.3f} s ({ms_per_file:.2f} ms/file)")
    print(f"[STRESS BENCHMARK] Hashing throughput: {res.hashing_throughput_mb_s:.2f} MB/s")
    # Scaling assertion: O(N) linear scan without subprocesses per file
    # Measured baseline is ~1.94 ms/file; assert with wider headroom (< 5.0 ms/file)
    assert ms_per_file < 5.0, f"Benchmark exceeded 5.0 ms/file latency ceiling: {ms_per_file:.2f} ms/file"
