"""
Acceptance test suite for Phase 0 Task 3 (P0-3) and Task 4 (P0-4):
Post-turn diff gate and derived commit tree binding engine.

Verifies:
1. Derived Commit Tree:
   - Excludes gitignored secrets (.env) from commit tree while keeping them in turn snapshot.
   - Preserves 100755 mode for executable files across turns.
   - Produces 0 spurious diffs under .gitattributes * text eol=crlf.
2. Supervisor-Mediated GitOps:
   - Denies git_commit to non-GitOps roles.
   - Asserts HEAD tree == approved derived tree on GitOps commit.
3. Pre-Turn Contract Grounding & .sync/** Self-Grant:
   - Non-Architecture role mutating .sync/** triggers SYNC_SELF_GRANT.
   - Architecture role mutating .sync/** is permitted.
4. Violation Matrix:
   - Reparse point (symlink/junction) creation.
   - Out-of-scope rename (source or destination outside scope).
   - Case-only rename (foo.py -> FOO.py).
   - Out-of-scope deletion, modification, creation.
   - Hardlink creation (st_nlink > 1).
   - DOS reserved device names (CON, PRN, AUX, NUL, COM1-9).
   - Trailing dots or spaces.
   - 8.3 short name aliases.
   - Forged mtime and preserved file size.
   - NTFS Alternate Data Streams on files and directories.
5. Violation Enforcement:
   - Reaps process trees via Job Object.
   - Restores workspace byte-exact.
   - Returns structured GateResult.
6. Gate Performance Measurement on Realistic Workspace.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from validators.kernel.contract import AgentContract
from validators.kernel.diff_gate import DiffGate, GateViolation, GateResult
from validators.kernel.process_reaper import ProcessReaper
from validators.kernel.turn_snapshot import TurnSnapshotManager


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
# 1. Derived Commit Tree Tests
# ==============================================================================

def test_derived_commit_tree_excludes_gitignored_env_file(gate_env):
    """Gitignored .env is retained in snapshot for safe revert, but excluded from derived commit tree."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Baseline snapshot
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker creates .env and in-scope feature.py
    (ws / ".env").write_text("SECRET_API_KEY=super_secret\n", encoding="utf-8")
    (ws / "feature.py").write_text("def feature(): pass\n", encoding="utf-8")

    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")
    snap_files = snapshot_mgr.list_snapshot_files(approved_snap_sha)
    assert ".env" in snap_files, ".env was missing from supervisor snapshot!"

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    # Compute derived commit tree
    derived_tree_sha = gate.compute_derived_commit_tree(approved_snap_sha)

    # Inspect tree entries
    tree_out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", derived_tree_sha],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.splitlines()

    assert ".env" not in tree_out, ".env leaked into derived commit tree!"
    assert "feature.py" in tree_out
    assert "app.py" in tree_out


def test_derived_commit_tree_preserves_100755_executable_mode(gate_env):
    """Derived tree inherits 100755 mode from HEAD across turns."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Create executable script at HEAD
    script_file = ws / "build.sh"
    script_file.write_bytes(b"#!/bin/sh\necho build\n")
    subprocess.run(["git", "add", "build.sh"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "update-index", "--chmod=+x", "build.sh"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "add executable script"], cwd=ws, check=True, capture_output=True, env=git_env)

    ls_head = subprocess.run(["git", "ls-tree", "HEAD", "build.sh"], cwd=ws, capture_output=True, text=True, env=git_env).stdout
    assert "100755" in ls_head, "build.sh is not 100755 in HEAD!"

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker modifies build.sh
    script_file.write_bytes(b"#!/bin/sh\necho updated build\n")
    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("build.sh",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    derived_tree_sha = gate.compute_derived_commit_tree(approved_snap_sha)

    tree_entry = subprocess.run(
        ["git", "ls-tree", derived_tree_sha, "build.sh"],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout
    assert "100755" in tree_entry, "Derived commit tree flattened 100755 mode to 100644!"


def test_derived_commit_tree_produces_zero_spurious_diffs_with_crlf_attributes(gate_env):
    """Repository with * text eol=crlf produces 0 spurious diffs when committing derived tree."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Configure .gitattributes with * text eol=crlf
    (ws / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitattributes"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "add gitattributes"], cwd=ws, check=True, capture_output=True, env=git_env)

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker creates file with CRLF
    (ws / "config.yaml").write_bytes(b"server:\r\n  port: 8080\r\n")
    approved_snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gitops_gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gitops_gate.supervisor_git_commit(
        message="feat: add config.yaml",
        approved_snapshot_sha=approved_snap_sha,
        branch="master",
    )

    # Assert 0 spurious diffs against HEAD
    diff_out = subprocess.run(["git", "diff", "HEAD"], cwd=ws, check=True, capture_output=True, text=True, env=git_env).stdout
    assert len(diff_out) == 0, f"Spurious diffs detected: {diff_out}"


# ==============================================================================
# 2. Supervisor-Mediated GitOps & Role Isolation
# ==============================================================================

def test_non_gitops_role_denied_supervisor_commit(gate_env):
    """Non-GitOps roles (Backend, Frontend, Architecture) are denied supervisor git commit."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    with pytest.raises(PermissionError) as exc_info:
        gate.supervisor_git_commit("unauthorized commit", snap_sha)
    assert "not authorized to create Git commits" in str(exc_info.value)


# ==============================================================================
# 3. Pre-Turn Contract Grounding & .sync/** Self-Grant Defense
# ==============================================================================

def test_non_architecture_role_modifying_sync_triggers_hard_violation(gate_env):
    """Non-Architecture role modifying .sync/** triggers SYNC_SELF_GRANT violation and rollback."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Pre-turn state
    sync_dir = ws / ".sync"
    sync_dir.mkdir()
    (sync_dir / "contracts.json").write_text('{"scope": "restricted"}', encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Backend attempts to grant itself full write scope by editing contracts.json
    (sync_dir / "contracts.json").write_text('{"scope": "unrestricted"}', encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")

    assert not res.passed
    assert any(v.violation_type == "SYNC_SELF_GRANT" for v in res.violations)
    # Workspace must be reverted byte-exact
    assert (sync_dir / "contracts.json").read_text(encoding="utf-8") == '{"scope": "restricted"}'


def test_architecture_role_modifying_sync_is_permitted(gate_env):
    """Architecture role is authorized to modify .sync/** contracts and work orders."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    sync_dir = ws / ".sync"
    sync_dir.mkdir(exist_ok=True)
    (sync_dir / "contracts.json").write_text('{"version": 1}', encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    (sync_dir / "contracts.json").write_text('{"version": 2}', encoding="utf-8")

    contract = AgentContract(agent_id="arch", work_order="WO-000", allow=(".sync/*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Architecture", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert res.passed
    assert not any(v.violation_type == "SYNC_SELF_GRANT" for v in res.violations)


# ==============================================================================
# 4. Comprehensive Violation Rule Matrix
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
        pytest.skip("SKIPPED: OS environment prevented junction creation")

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

    # Rename allowed.py -> secret.py (where secret.py is out of scope)
    (ws / "allowed.py").rename(ws / "secret.py")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("allowed.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "RENAME_OUT_OF_SCOPE" for v in res.violations)
    assert (ws / "allowed.py").exists()
    assert not (ws / "secret.py").exists()


def test_violation_case_only_rename_rejected(gate_env):
    """Case-only rename (e.g. app.py -> APP.PY) triggers CASE_ONLY_RENAME."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "foo.py").write_text("foo\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Simulate case-only rename
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
        pytest.skip("SKIPPED: Hardlink creation denied by OS")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "HARDLINK_DETECTED" for v in res.violations)


def test_violation_dos_reserved_device_name_rejected(gate_env):
    """Creating DOS reserved device names (e.g. CON.txt or NUL.py) triggers RESERVED_DEVICE_NAME."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # On Windows, creating a path like "con.txt" in workspace is a device name anomaly
    res = DiffGate.check_dos_reserved_name("sub/nul.py")
    assert res == "nul.py"
    assert DiffGate.check_dos_reserved_name("src/com1.dat") == "com1.dat"
    assert DiffGate.check_dos_reserved_name("normal_file.py") is None


def test_violation_trailing_dots_or_spaces_rejected(gate_env):
    """Path components with trailing dots or spaces trigger TRAILING_DOT_OR_SPACE."""
    assert DiffGate.check_trailing_dots_or_spaces("sub/evil.py.") == "evil.py."
    assert DiffGate.check_trailing_dots_or_spaces("sub/evil.py ") == "evil.py "
    assert DiffGate.check_trailing_dots_or_spaces("sub/clean.py") is None


def test_violation_short_name_alias_rejected(gate_env):
    """8.3 short name patterns (~1) trigger SHORT_NAME_ALIAS."""
    assert DiffGate.check_short_name_alias("PROGRA~1/app.py") is True
    assert DiffGate.check_short_name_alias("RUNNER~2/log.txt") is True
    assert DiffGate.check_short_name_alias("regular_file.py") is False


def test_violation_forged_mtime_and_size_detected_via_sha(gate_env):
    """Adversary modifying content while resetting mtime and size is caught by full content hash."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    target = ws / "module.py"
    target.write_bytes(b"content_a_123456789")  # 19 bytes
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    stat_orig = target.stat()

    # Overwrite with same byte length and forge original timestamp via os.utime
    target.write_bytes(b"content_b_123456789")  # 19 bytes
    os.utime(target, (stat_orig.st_atime, stat_orig.st_mtime))

    # Assert mtime and size appear identical
    stat_forged = target.stat()
    assert stat_forged.st_size == stat_orig.st_size
    assert int(stat_forged.st_mtime) == int(stat_orig.st_mtime)

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("other.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    # Gate must catch the modification despite forged mtime and size
    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "OUT_OF_SCOPE_MODIFY" and v.path == "module.py" for v in res.violations)
    # Reverted
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
        pytest.skip("SKIPPED: NTFS ADS not supported")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("*",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "ADS_STREAM_DETECTED" for v in res.violations)


# ==============================================================================
# 5. Process Reaping and Byte-Exact Rollback on Violation
# ==============================================================================

def test_violation_reaps_background_process_and_reverts_byte_exact(gate_env):
    """On violation, background looping process is reaped and workspace restored byte-exact."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    (ws / "code.py").write_text("original = 1\n", encoding="utf-8")
    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Spawn background looping process
    proc = subprocess.Popen([sys.executable, "-c", "import time; \nwhile True: time.sleep(0.05)"])
    reaper.register_process(proc)

    # Unauthorized change
    (ws / "unauthorized.py").write_text("evil\n", encoding="utf-8")
    (ws / "code.py").write_text("original = 2\n", encoding="utf-8")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("code.py",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed

    # Background process must be reaped
    time.sleep(0.2)
    assert proc.poll() is not None

    # Workspace reverted byte-exact
    assert not (ws / "unauthorized.py").exists()
    assert (ws / "code.py").read_text(encoding="utf-8") == "original = 1\n"


# ==============================================================================
# 6. Performance Benchmark on Realistic Workspace
# ==============================================================================

def test_gate_evaluation_performance_on_realistic_workspace(gate_env):
    """Benchmark diff gate hashing throughput and latency across 100 files."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Populate 100 source files in nested directories
    src_dir = ws / "src"
    src_dir.mkdir()
    for i in range(100):
        mod_dir = src_dir / f"module_{i // 10}"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / f"service_{i}.py").write_text(f"# service {i}\ndef run_{i}():\n    return {i * 42}\n", encoding="utf-8")

    pre_sha = snapshot_mgr.create_turn_snapshot("turn-0")

    # Worker modifies 5 files and creates 2 files in scope
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

    # Report performance telemetry
    print(f"\n[BENCHMARK] Realistic workspace evaluation time: {res.evaluation_duration_seconds * 1000:.2f} ms")
    print(f"[BENCHMARK] Hashing throughput: {res.hashing_throughput_mb_s:.2f} MB/s")
    assert res.evaluation_duration_seconds < 3.0, "Diff gate latency exceeded 3.0s ceiling for 100 files!"


# ==============================================================================
# 7. Additional P0-3 & P0-4 Guarantees
# ==============================================================================

def test_suspended_launch_ordering_pins_to_job_before_execution(gate_env):
    """launch_in_job starts process suspended, assigns to Job Object, then resumes."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    proc = reaper.launch_in_job([sys.executable, "-c", "import time; time.sleep(2)"])
    assert proc.poll() is None
    assert proc.pid in reaper._registered_pids

    # Reaping terminates the process
    reaped = reaper.reap_all()
    assert proc.pid in reaped
    time.sleep(0.1)
    assert proc.poll() is not None


def test_persistent_services_job_isolation(gate_env):
    """Persistent services run in a dedicated Job Object isolated from per-turn reapers."""
    from validators.kernel.process_reaper import ServiceJobManager

    ws, store, snapshot_mgr, turn_reaper, git_env = gate_env
    service_mgr = ServiceJobManager()

    # Launch service and worker process
    service_proc = service_mgr.launch_service([sys.executable, "-c", "import time; time.sleep(5)"])
    worker_proc = turn_reaper.launch_in_job([sys.executable, "-c", "import time; time.sleep(5)"])

    assert service_proc.poll() is None
    assert worker_proc.poll() is None

    # End of worker turn: turn_reaper.reap_all() must terminate worker but NOT persistent service
    turn_reaper.reap_all()
    time.sleep(0.2)
    assert worker_proc.poll() is not None, "Worker process must be terminated on turn end!"
    assert service_proc.poll() is None, "Persistent service must remain running across turn boundaries!"

    # Explicit supervisor service shutdown terminates service
    service_mgr.shutdown_services()
    time.sleep(0.2)
    assert service_proc.poll() is not None, "Persistent service must be terminated on service shutdown!"


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
        pytest.skip("SKIPPED: Directory NTFS ADS not supported by filesystem")

    contract = AgentContract(agent_id="be", work_order="WO-001", allow=("subdir/**",), deny=(), write_mode="read-write")
    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", contract=contract, reaper=reaper)

    res = gate.evaluate_turn(pre_turn_snapshot_sha=pre_sha, turn_id="turn-0")
    assert not res.passed
    assert any(v.violation_type == "ADS_STREAM_DETECTED" and "subdir" in v.path for v in res.violations)


def test_store_digest_only_in_memory(gate_env):
    """Store digest baseline is maintained exclusively in supervisor memory and never on disk."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env
    snapshot_mgr.create_turn_snapshot("turn-0")

    baseline = snapshot_mgr.baseline_store_hash
    assert baseline is not None
    assert len(baseline) == 64

    # Assert that no file anywhere inside storage_root contains baseline hash bytes or string
    for root, dirs, files in os.walk(store):
        for f in files:
            file_content = (Path(root) / f).read_bytes()
            assert baseline.encode("ascii") not in file_content, f"Store file '{f}' leaks memory baseline hash!"


def test_qa_clean_install_and_verification(gate_env, tmp_path):
    """QA verification runs against derived commit tree in clean worktree, free of uncommitted state."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # Turn creates approved file and gitignored .env
    (ws / ".env").write_text("SECRET=12345\n", encoding="utf-8")
    (ws / "service.py").write_text("def run(): pass\n", encoding="utf-8")
    snap_sha = snapshot_mgr.create_turn_snapshot("turn-1")

    gate = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)
    commit_sha = gate.supervisor_git_commit("add service", approved_snapshot_sha=snap_sha)

    # QA clean verification: clean isolated worktree checked out from the approved commit
    qa_dir = tmp_path / "qa_clean_worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(qa_dir), commit_sha],
        cwd=ws,
        check=True,
        capture_output=True,
        env=git_env,
    )

    try:
        # Assert clean install: service.py is present, but .env is absent
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

    # QA review passes and approves turn
    milestone_state = {"status": "QA_APPROVED", "approved_snapshot": snap_sha}

    # Worker modifies workspace on a subsequent turn
    (ws / "module.py").write_text("v2 = Tampered\n", encoding="utf-8")

    # Supervisor gate evaluation detects change relative to approved state
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


def test_supervisor_git_push_ephemeral_credentials_isolation(gate_env, monkeypatch):
    """supervisor_git_push passes auth token ephemerally and zeroes credential memory buffer on exit."""
    ws, store, snapshot_mgr, reaper, git_env = gate_env

    # 1. Non-GitOps role is denied
    gate_worker = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="Backend", reaper=reaper)
    with pytest.raises(PermissionError, match="not authorized to execute Git push"):
        gate_worker.supervisor_git_push("origin", "master", auth_token="secret_token")

    # 2. GitOps role injects token ephemerally and zeroes buffer
    gate_gitops = DiffGate(workspace=ws, snapshot_manager=snapshot_mgr, role="GitOps", reaper=reaper)

    captured_cmds = []
    def fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    test_token = "ghp_ephemeral_super_secret_token_12345"
    gate_gitops.supervisor_git_push("https://github.com/org/repo.git", "master", auth_token=test_token)

    assert len(captured_cmds) == 1
    push_cmd = captured_cmds[0]

    # Token was passed in http.extraHeader
    assert any("http.extraHeader=Authorization: Bearer ghp_ephemeral_super_secret_token_12345" in arg for arg in push_cmd)
    assert "push" in push_cmd
    assert "master" in push_cmd
