"""
Tests for Phase 0 Task 2 (P0-2): Per-turn snapshot and safe revert.

Acceptance criteria:
1. Snapshots and rescue refs are stored in a supervisor-managed bare git repository
   outside the agent-writable workspace, not inside workspace .git.
2. Nested .git repositories or gitfiles planted to hide files via submodules are detected and rejected.
3. Edits to workspace .gitignore cannot hide files from turn snapshots (force add + supervisor excludes).
4. Restore removes symlinks before checkout to prevent symlink traversal attacks writing outside workspace.
5. Files with CRLF line endings round-trip byte-exact without autocrlf corruption.
6. Deleted or tampered snapshot stores/refs are detected against supervisor memory/signed state.
7. Reverting a turn does not move workspace HEAD or touch workspace branch refs.
"""

from pathlib import Path
import os
import subprocess
import sys
import pytest

from validators.kernel.turn_snapshot import (
    TurnSnapshotManager,
    NestedGitError,
    SnapshotIntegrityError,
)


@pytest.fixture
def test_env(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = tmp_path / "supervisor_storage" / "snapshots.git"
    
    # Initialize workspace as a git repo with a commit
    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": null_dev,
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
    
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, env=git_env).stdout.strip()
    
    manager = TurnSnapshotManager(workspace=ws, storage_root=store)
    return ws, store, manager, head_sha, git_env


def test_nested_git_init_detected_and_rejected(test_env):
    ws, store, manager, head_sha, git_env = test_env
    
    # Adversary plants a nested repository to create a gitlink and hide files
    sub_dir = ws / "submodule_trick"
    sub_dir.mkdir()
    (sub_dir / "hidden.py").write_text("malicious code\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=sub_dir, check=True, capture_output=True, env=git_env)
    
    with pytest.raises(NestedGitError) as exc_info:
        manager.create_turn_snapshot("turn-1")
    assert "Nested git repository or gitfile detected" in str(exc_info.value)


def test_gitignore_edit_cannot_hide_files(test_env):
    ws, store, manager, head_sha, git_env = test_env
    
    # Create legitimate file
    (ws / "app.py").write_text("print('app')\n", encoding="utf-8")
    
    # Agent edits .gitignore to ignore evil.py and creates evil.py
    (ws / ".gitignore").write_text("evil.py\n", encoding="utf-8")
    (ws / "evil.py").write_text("print('evil')\n", encoding="utf-8")
    
    # Snapshot must capture evil.py regardless of .gitignore
    snap_sha = manager.create_turn_snapshot("turn-1")
    snap_files = manager.list_snapshot_files(snap_sha)
    assert "evil.py" in snap_files, "evil.py was hidden from snapshot by .gitignore!"
    assert "app.py" in snap_files


def test_symlink_traversal_prevented_on_restore(test_env):
    ws, store, manager, head_sha, git_env = test_env
    
    # Baseline file
    (ws / "clean.txt").write_text("clean\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")
    
    # Agent plants a symlink/junction pointing outside workspace (to tmp_path)
    outside_target = ws.parent / "outside_target.txt"
    outside_target.write_text("do not overwrite\n", encoding="utf-8")
    
    symlink_path = ws / "escape_link"
    try:
        symlink_path.symlink_to(outside_target)
    except (OSError, NotImplementedError):
        # On Windows without developer mode/admin symlink privs, create a directory junction or skip link creation
        pytest.skip("Symlink creation not permitted in this OS environment")
        
    assert symlink_path.is_symlink()
    
    # Restore must lstat and remove the symlink before writing
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert not symlink_path.exists(), "Symlink was not removed before restore!"
    assert outside_target.read_text(encoding="utf-8") == "do not overwrite\n"


def test_crlf_roundtrip_byte_exact(test_env):
    ws, store, manager, head_sha, git_env = test_env
    
    # File with exact Windows CRLF sequences
    crlf_bytes = b"server_config:\r\n  port: 8080\r\n  debug: false\r\n"
    target_file = ws / "config.yaml"
    target_file.write_bytes(crlf_bytes)
    
    snap_sha = manager.create_turn_snapshot("turn-1")
    
    # Corrupt the file
    target_file.write_bytes(b"corrupted\n")
    
    # Restore
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert target_file.read_bytes() == crlf_bytes, "CRLF content did not round-trip byte-exact!"


def test_deleted_or_tampered_store_detected(test_env):
    ws, store, manager, head_sha, git_env = test_env
    
    (ws / "valid.txt").write_text("valid\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")
    
    # Tampering: someone passes an altered or non-existent expected SHA
    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha="deadbeef" * 5)
    assert "Snapshot integrity verification failed" in str(exc_info.value)


def test_non_head_revert_does_not_move_workspace_head(test_env):
    ws, store, manager, initial_head_sha, git_env = test_env
    
    (ws / "feature.py").write_text("feature v1\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")
    
    # Agent creates unwanted changes
    (ws / "feature.py").write_text("broken feature\n", encoding="utf-8")
    (ws / "unwanted.py").write_text("unwanted\n", encoding="utf-8")
    
    # Revert
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    
    # Working tree is restored
    assert (ws / "feature.py").read_text(encoding="utf-8") == "feature v1\n"
    assert not (ws / "unwanted.py").exists()
    
    # Workspace HEAD remains unchanged
    current_head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, env=git_env).stdout.strip()
    assert current_head_sha == initial_head_sha, "Workspace HEAD was moved during turn revert!"
