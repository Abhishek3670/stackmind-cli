"""
Comprehensive acceptance tests for Phase 0 Task 2 (P0-2): Per-turn snapshot and safe revert.

Acceptance criteria:
1. Filter pipeline bypass: workspace .gitattributes (eol, ident, filters) cannot alter
   snapshot or restore; files round-trip byte-exact.
2. Untracked file and directory cleanup: new files and created directories are wiped on revert.
3. Process-tree reaping: background processes attempting to write/race during restore are killed.
4. Store config integrity: tampering with <store>/config (e.g. injecting fsmonitor) raises
   SnapshotIntegrityError and prevents execution.
5. Host substrate & symlink handling: Windows substrate documented; symlinks/reparse points
   unlinked before restore (or skipped with explicit status if OS denies link creation).
6. Nested .git detection: post-turn detection catches nested git repos/gitfiles in tracked code;
   allows nested git inside supervisor-excluded dependency dirs (node_modules, .venv).
7. Store integrity failures: deleting store dir, deleting ref, or corrupting objects raises
   SnapshotIntegrityError.
8. Non-HEAD revert: workspace .git HEAD and index remain untouched across snapshots and restores.
9. Rescue ref: preserved under refs/stackmind/rescue/<run_id>.
10. NTFS Alternate Data Streams (ADS): destroyed on restore via unlinking.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from validators.kernel.process_reaper import ProcessReaper
from validators.kernel.turn_snapshot import (
    TurnSnapshotManager,
    NestedGitError,
    SnapshotIntegrityError,
    DEFAULT_SUPERVISOR_EXCLUSIONS,
)


@pytest.fixture
def test_env(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = tmp_path / "supervisor_storage" / "snapshots.git"

    # Initialize workspace as a git repo with an initial commit
    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": null_dev,
        "GIT_CONFIG_SYSTEM": null_dev,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Author",
        "GIT_AUTHOR_EMAIL": "author@test.com",
        "GIT_COMMITTER_NAME": "Committer",
        "GIT_COMMITTER_EMAIL": "committer@test.com",
    }
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True, env=git_env)
    initial_file = ws / "initial.txt"
    initial_file.write_text("initial commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "initial.txt"], cwd=ws, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True, env=git_env)

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()

    reaper = ProcessReaper()
    manager = TurnSnapshotManager(workspace=ws, storage_root=store, reaper=reaper)
    return ws, store, manager, head_sha, git_env, reaper


# ==============================================================================
# 1. Filter Pipeline Bypass Tests (.gitattributes eol, ident, filters)
# ==============================================================================

def test_gitattributes_filter_bypass_roundtrips_byte_exact(test_env):
    """Workspace .gitattributes with eol=crlf and ident must not mutate file bytes."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    # Plant .gitattributes with aggressive eol and ident rules
    (ws / ".gitattributes").write_text("* text eol=crlf\n*.txt ident\n", encoding="utf-8")

    # File with strict LF line endings and $Id$ ident token
    lf_content = b"first line\nsecond line\n$Id: 12345$\n"
    target_file = ws / "module.txt"
    target_file.write_bytes(lf_content)

    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent corrupts the file during turn
    target_file.write_bytes(b"corrupted\r\n")

    # Revert turn
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Restored bytes must match original LF content byte-for-byte
    assert target_file.read_bytes() == lf_content, "Filter pipeline altered file during snapshot/restore!"


# ==============================================================================
# 2. Untracked File & Directory Cleanup on Revert
# ==============================================================================

def test_restore_deletes_untracked_files_and_emptied_dirs(test_env):
    """Restore must unlink newly created untracked files and remove emptied directories."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "base.py").write_text("print('base')\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent creates new files in a nested directory
    new_sub = ws / "new_module" / "deep_sub"
    new_sub.mkdir(parents=True)
    new_file = new_sub / "untracked.py"
    new_file.write_text("malicious()\n", encoding="utf-8")
    root_untracked = ws / "temp_artifact.log"
    root_untracked.write_text("log data\n", encoding="utf-8")

    assert new_file.exists()
    assert (ws / "new_module").exists()

    # Revert turn
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Files must be deleted
    assert not new_file.exists(), "Untracked file in subfolder was not deleted!"
    assert not root_untracked.exists(), "Untracked root file was not deleted!"
    # Emptied directory hierarchy must be removed
    assert not (ws / "new_module").exists(), "Emptied directory was not removed bottom-up!"
    assert (ws / "base.py").read_text(encoding="utf-8") == "print('base')\n"


# ==============================================================================
# 3. Process-Tree Kill as Precondition to Diff & Restore
# ==============================================================================

def test_process_tree_kill_precondition_blocks_restore_race(test_env):
    """Background processes attempting to race or plant files during restore are reaped."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "file.txt").write_text("original\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent spawns a background looping process that tries to constantly overwrite file.txt
    race_script = (
        "import time, sys\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "while True:\n"
        "    try:\n"
        "        p.write_text('raced!\\n')\n"
        "    except Exception:\n"
        "        pass\n"
        "    time.sleep(0.01)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", race_script, str(ws / "file.txt")],
    )
    reaper.register_pid(proc.pid)

    # Give process time to start writing
    time.sleep(0.1)

    # Restore must reap the process tree before writing
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Process must be terminated
    assert proc.poll() is not None, "Background process was not reaped by restore!"
    assert (ws / "file.txt").read_text(encoding="utf-8") == "original\n"


# ==============================================================================
# 4. External Store Config Integrity & Injection Defense
# ==============================================================================

def test_store_config_tampering_raises_integrity_error(test_env):
    """Adversary modifying <store>/config (e.g. fsmonitor injection) is detected and blocked."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "valid.txt").write_text("valid\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Malicious agent tampers with external store config
    config_file = store / "config"
    with open(config_file, "a", encoding="utf-8") as f:
        f.write("\n[core]\n\tfsmonitor = powershell -c calc.exe\n")

    # Subsequent snapshot or restore must fail fast with SnapshotIntegrityError
    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "store config tampering detected" in str(exc_info.value).lower()

    # create_turn_snapshot also blocks
    with pytest.raises(SnapshotIntegrityError):
        manager.create_turn_snapshot("turn-2")


# ==============================================================================
# 5. Nested .git Detection (Pre-turn, Post-turn & Dependency Exclusions)
# ==============================================================================

def test_nested_git_in_source_tree_detected_and_rejected(test_env):
    """Nested git repository in tracked source code raises NestedGitError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    sub_dir = ws / "sub_repo"
    sub_dir.mkdir()
    subprocess.run(["git", "init"], cwd=sub_dir, check=True, capture_output=True, env=git_env)

    with pytest.raises(NestedGitError) as exc_info:
        manager.create_turn_snapshot("turn-1")
    assert "Nested git repository or gitfile detected" in str(exc_info.value)


def test_nested_git_in_node_modules_is_permitted(test_env):
    """Nested git repository inside supervisor-excluded dependency dir is ignored."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    # Legitimate package in node_modules containing a .git dir (e.g. git dependency)
    dep_git = ws / "node_modules" / "some_pkg" / ".git"
    dep_git.mkdir(parents=True)
    (dep_git / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")

    (ws / "src").mkdir()
    (ws / "src" / "index.js").write_text("console.log('hi');\n", encoding="utf-8")

    # Snapshot must succeed and not raise NestedGitError
    snap_sha = manager.create_turn_snapshot("turn-1")
    snap_files = manager.list_snapshot_files(snap_sha)
    assert "src/index.js" in snap_files
    assert not any("node_modules" in f for f in snap_files)


def test_nested_git_planted_during_turn_detected_post_turn(test_env):
    """Nested git directory planted during a turn is detected post-turn on restore."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "app.py").write_text("print('app')\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent plants nested git during the turn
    evil_dir = ws / "stealth"
    evil_dir.mkdir()
    subprocess.run(["git", "init"], cwd=evil_dir, check=True, capture_output=True, env=git_env)

    # Post-turn check catches it
    with pytest.raises(NestedGitError):
        manager.check_nested_git()


# ==============================================================================
# 6. Store Directory Deletion, Ref Deletion, and Object Corruption Tests
# ==============================================================================

def test_deleted_store_directory_raises_integrity_error(test_env):
    """Deleting the external store directory raises SnapshotIntegrityError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "test.txt").write_text("test\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # External store directory is deleted
    def _force_rm_readonly(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    shutil.rmtree(store, onexc=_force_rm_readonly)

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "missing or deleted" in str(exc_info.value).lower()


def test_deleted_ref_raises_integrity_error(test_env):
    """Deleting a snapshot ref in the external store raises SnapshotIntegrityError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "test.txt").write_text("test\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Manually delete the ref file from store
    ref_file = store / "refs" / "stackmind" / "turn" / "turn-1"
    if ref_file.exists():
        ref_file.unlink()
    else:
        # If packed-refs, truncate it
        packed = store / "packed-refs"
        if packed.exists():
            packed.unlink()

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "ref 'refs/stackmind/turn/turn-1' not found" in str(exc_info.value)


def test_corrupted_object_raises_integrity_error(test_env):
    """Corrupting a blob object in the store raises SnapshotIntegrityError on restore."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "payload.txt").write_text("precious payload\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    snap_files = manager.list_snapshot_files(snap_sha)
    blob_sha = snap_files["payload.txt"]

    # Locate and corrupt loose blob object
    obj_path = store / "objects" / blob_sha[:2] / blob_sha[2:]
    assert obj_path.exists()
    os.chmod(obj_path, stat.S_IWRITE)
    obj_path.write_bytes(b"corrupted garbage bytes that break git decompression")

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "corrupt or unreadable" in str(exc_info.value).lower()


# ==============================================================================
# 7. Symlink & Junction Traversal Neutralization
# ==============================================================================

def test_symlink_traversal_neutralized(test_env):
    """Planted symlinks/junctions outside the workspace are neutralized before restore."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "clean.txt").write_text("clean\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    outside_target = ws.parent / "sensitive_outside.txt"
    outside_target.write_text("do not overwrite\n", encoding="utf-8")

    link_path = ws / "escape_link"
    try:
        link_path.symlink_to(outside_target)
        ran_status = "RAN AGAINST CODE"
    except (OSError, NotImplementedError):
        # On Windows without developer mode/admin symlink privilege, WinError 5 occurs
        ran_status = "SKIPPED: OS sandbox denies symlink creation (WinError 5)"
        pytest.skip(ran_status)

    assert link_path.is_symlink()

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert not link_path.exists(), "Symlink was not neutralized before restore!"
    assert outside_target.read_text(encoding="utf-8") == "do not overwrite\n"


# ==============================================================================
# 8. Rename, Delete, and Chmod Restore
# ==============================================================================

def test_rename_delete_restore_cycle(test_env):
    """Restores full state when files are renamed, deleted, or altered."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "keep.txt").write_text("keep v1\n", encoding="utf-8")
    (ws / "delete_me.txt").write_text("delete v1\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent renames keep.txt -> moved.txt, deletes delete_me.txt, creates new.txt
    (ws / "keep.txt").rename(ws / "moved.txt")
    (ws / "delete_me.txt").unlink()
    (ws / "new.txt").write_text("new\n", encoding="utf-8")

    assert not (ws / "keep.txt").exists()
    assert (ws / "moved.txt").exists()

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    assert (ws / "keep.txt").exists()
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "keep v1\n"
    assert (ws / "delete_me.txt").exists()
    assert (ws / "delete_me.txt").read_text(encoding="utf-8") == "delete v1\n"
    assert not (ws / "moved.txt").exists()
    assert not (ws / "new.txt").exists()


# ==============================================================================
# 9. Workspace .git Index and HEAD Remain Untouched
# ==============================================================================

def test_workspace_git_index_and_head_untouched_by_revert(test_env):
    """Reverting a turn does not touch workspace HEAD, branch refs, or .git/index."""
    ws, store, manager, initial_head_sha, git_env, reaper = test_env

    index_path = ws / ".git" / "index"
    initial_index_bytes = index_path.read_bytes()

    (ws / "feature.py").write_text("v1\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Agent modifies working tree
    (ws / "feature.py").write_text("v2\n", encoding="utf-8")
    (ws / "stray.py").write_text("stray\n", encoding="utf-8")

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Working tree restored
    assert (ws / "feature.py").read_text(encoding="utf-8") == "v1\n"
    assert not (ws / "stray.py").exists()

    # Workspace HEAD unchanged
    curr_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()
    assert curr_head == initial_head_sha, "Workspace HEAD was moved during turn revert!"

    # Workspace .git/index unchanged
    assert index_path.read_bytes() == initial_index_bytes, "Workspace .git/index was modified by revert!"


# ==============================================================================
# 10. Rescue Ref Preserved on Abort
# ==============================================================================

def test_rescue_ref_preserved_on_abort(test_env):
    """Aborted runs preserve their workspace state under refs/stackmind/rescue/<run_id>."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "work_in_progress.py").write_text("half_finished()\n", encoding="utf-8")
    rescue_sha = manager.create_rescue_ref("run-abort-99")

    # Verify rescue ref exists in external store
    res = subprocess.run(
        [
            "git",
            "--git-dir", str(store),
            "rev-parse", "--verify", "refs/stackmind/rescue/run-abort-99",
        ],
        capture_output=True,
        text=True,
        env=git_env,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == rescue_sha

    # Verify rescue ref contains the uncommitted file
    files = manager.list_snapshot_files(rescue_sha)
    assert "work_in_progress.py" in files


# ==============================================================================
# 11. NTFS Alternate Data Streams (ADS) Destroyed on Restore
# ==============================================================================

def test_ntfs_alternate_data_streams_destroyed_on_restore(test_env):
    """Restoring a file unlinks it first, destroying any attached NTFS ADS streams."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    target_file = ws / "config.ini"
    target_file.write_text("[main]\nmode=prod\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # On Windows NTFS, attach an alternate data stream
    ads_path = ws / "config.ini:hidden_stream"
    try:
        ads_path.write_text("stealth payload", encoding="utf-8")
        ads_supported = True
    except (OSError, NotImplementedError):
        ads_supported = False

    if not ads_supported:
        pytest.skip("NTFS Alternate Data Streams not supported on this filesystem")

    assert ads_path.read_text(encoding="utf-8") == "stealth payload"

    # Revert turn
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Base file restored
    assert target_file.read_text(encoding="utf-8") == "[main]\nmode=prod\n"

    # ADS stream must be completely eradicated
    assert not ads_path.exists(), "NTFS Alternate Data Stream persisted across turn restore!"
