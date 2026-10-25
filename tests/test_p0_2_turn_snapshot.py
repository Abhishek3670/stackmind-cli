"""
Comprehensive acceptance tests for Phase 0 Task 2 (P0-2): Per-turn snapshot and safe revert.

Acceptance criteria:
1. Filter pipeline bypass: workspace .gitattributes (eol, ident, filters) cannot alter
   snapshot or restore; files round-trip byte-exact.
2. Untracked file and directory cleanup: new files and created directories are wiped on revert.
3. Process-tree reaping: Job Object (KILL_ON_JOB_CLOSE, no breakaway) kills runaway grandchildren
   even when intermediate parent process has already exited.
4. Store config and metadata integrity: tampering with <store>/config, <store>/hooks/, or
   <store>/objects/info/alternates raises SnapshotIntegrityError; objects only grow monotonically.
5. Host substrate & symlink/junction handling: Windows substrate documented; junction/reparse points
   are neutralized without recursive descent into the target folder.
6. Untracked cleanup non-descending: cleanup walk does NOT recurse into planted junctions/symlinks;
   outside files remain intact.
7. Workspace root-anchored exclusions: files under src/node_modules and __pycache__ are captured.
8. Post-turn nested .git check: reaps processes before raising NestedGitError.
9. Non-default NTFS Alternate Data Streams (ADS): enumerated and eradicated on all files.
10. Workspace .git index and HEAD remain untouched across snapshots and restores.
11. Rescue ref: preserved under refs/stackmind/rescue/<run_id>.
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


def check_strict_skip(reason: str):
    """Enforce strict security policy: by default, any skip of a security-critical test is a failure."""
    if os.environ.get("ALLOW_SECURITY_SKIPS") == "1":
        pytest.skip(f"PERMITTED SKIP: {reason}")
    pytest.fail(f"STRICT SECURITY FAILURE (Strict mode default): {reason}")


@pytest.fixture
def test_env(tmp_path):
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

    (ws / ".gitattributes").write_text("* text eol=crlf\n*.txt ident\n", encoding="utf-8")

    lf_content = b"first line\nsecond line\n$Id: 12345$\n"
    target_file = ws / "module.txt"
    target_file.write_bytes(lf_content)

    snap_sha = manager.create_turn_snapshot("turn-1")

    target_file.write_bytes(b"corrupted\r\n")

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    assert target_file.read_bytes() == lf_content, "Filter pipeline altered file during snapshot/restore!"


# ==============================================================================
# 2. Workspace-Root-Anchored Exclusions & Bytecode Handling
# ==============================================================================

def test_file_under_src_node_modules_is_captured(test_env):
    """Exclusions are anchored strictly to workspace root; src/node_modules is captured."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    # 1. Root node_modules is excluded
    root_nm = ws / "node_modules" / "dep"
    root_nm.mkdir(parents=True)
    (root_nm / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    # 2. Nested src/node_modules is NOT excluded and must be captured
    nested_nm = ws / "src" / "node_modules" / "nested_dep"
    nested_nm.mkdir(parents=True)
    evil_js = nested_nm / "evil.js"
    evil_js.write_text("console.log('injected');\n", encoding="utf-8")

    # 3. Bytecode file inside src/__pycache__ is NOT excluded and must be captured
    pycache_dir = ws / "src" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    pyc_file = pycache_dir / "evil.cpython-313.pyc"
    pyc_file.write_bytes(b"\x55\x0d\x0d\x0a\x00\x00\x00\x00malicious_bytecode")

    snap_sha = manager.create_turn_snapshot("turn-1")
    snap_files = manager.list_snapshot_files(snap_sha)

    # Root node_modules must NOT be in snapshot
    assert not any(f.startswith("node_modules/") for f in snap_files)

    # Nested src/node_modules/evil.js MUST be captured
    assert "src/node_modules/nested_dep/evil.js" in snap_files, "src/node_modules was erroneously excluded!"

    # Bytecode file MUST be captured
    assert "src/__pycache__/evil.cpython-313.pyc" in snap_files, "Bytecode was erroneously ignored!"


# ==============================================================================
# 3. Untracked File & Directory Cleanup on Revert
# ==============================================================================

def test_restore_deletes_untracked_files_and_emptied_dirs(test_env):
    """Restore must unlink newly created untracked files and remove emptied directories."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "base.py").write_text("print('base')\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    new_sub = ws / "new_module" / "deep_sub"
    new_sub.mkdir(parents=True)
    new_file = new_sub / "untracked.py"
    new_file.write_text("malicious()\n", encoding="utf-8")
    root_untracked = ws / "temp_artifact.log"
    root_untracked.write_text("log data\n", encoding="utf-8")

    assert new_file.exists()
    assert (ws / "new_module").exists()

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    assert not new_file.exists(), "Untracked file in subfolder was not deleted!"
    assert not root_untracked.exists(), "Untracked root file was not deleted!"
    assert not (ws / "new_module").exists(), "Emptied directory was not removed bottom-up!"
    assert (ws / "base.py").read_text(encoding="utf-8") == "print('base')\n"


# ==============================================================================
# 4. Job Object Grandchild Process Reaping (Intermediate Exits)
# ==============================================================================

def test_job_object_grandchild_killed_after_intermediate_exits(test_env):
    """Intermediate process exits, grandchild keeps writing; Job Object still kills grandchild."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "target.txt").write_text("baseline\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Script where child spawns grandchild and child exits immediately
    child_code = (
        "import subprocess, sys, time\n"
        "target_file = sys.argv[1]\n"
        "grandchild_code = (\n"
        "    'import time, sys\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'p = Path(sys.argv[1])\\n'\n"
        "    'while True:\\n'\n"
        "    '    try:\\n'\n"
        "    '        p.write_text(\"grandchild_race\\n\")\\n'\n"
        "    '    except Exception:\\n'\n"
        "    '        pass\\n'\n"
        "    '    time.sleep(0.02)\\n'\n"
        ")\n"
        "g = subprocess.Popen([sys.executable, '-c', grandchild_code, target_file])\n"
        "sys.stdout.write(f'{g.pid}\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", child_code, str(ws / "target.txt")],
        stdout=subprocess.PIPE,
        text=True,
    )
    reaper.register_process(proc)

    # Read grandchild PID and wait for intermediate process to exit
    grandchild_pid = int(proc.stdout.readline().strip())
    proc.wait(timeout=5)
    assert proc.poll() == 0, "Intermediate process did not exit!"

    # Give grandchild time to write
    time.sleep(0.1)

    # Restore must terminate the Job Object, killing the orphaned grandchild
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Assert grandchild was killed
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = kernel32.OpenProcess(0x1000, False, grandchild_pid)
        if h:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            assert code.value != 259, "Grandchild is still alive after Job Object termination!"

    assert (ws / "target.txt").read_text(encoding="utf-8") == "baseline\n"


# ==============================================================================
# 5. External Store Metadata Integrity: Config, Hooks, Alternates & Object Growth
# ==============================================================================

def test_store_hooks_tampering_raises_integrity_error(test_env):
    """Adversary creating <store>/hooks/post-commit triggers SnapshotIntegrityError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "valid.txt").write_text("valid\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Malicious agent plants a hook script in external store
    hooks_dir = store / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_file = hooks_dir / "post-commit"
    hook_file.write_text("#!/bin/sh\ncalc.exe\n", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "tampering detected" in str(exc_info.value).lower()


def test_store_alternates_tampering_raises_integrity_error(test_env):
    """Adversary creating objects/info/alternates triggers SnapshotIntegrityError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "valid.txt").write_text("valid\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Malicious agent points store to an external alternates object pool
    info_dir = store / "objects" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    alternates_file = info_dir / "alternates"
    alternates_file.write_text("C:/evil_repo/objects\n", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "tampering detected" in str(exc_info.value).lower()


def test_deleted_store_directory_raises_integrity_error(test_env):
    """Deleting the external store directory raises SnapshotIntegrityError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "test.txt").write_text("test\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

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

    ref_file = store / "refs" / "stackmind" / "turn" / "turn-1"
    if ref_file.exists():
        ref_file.unlink()
    else:
        packed = store / "packed-refs"
        if packed.exists():
            packed.unlink()

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert (
        "ref 'refs/stackmind/turn/turn-1' not found" in str(exc_info.value)
        or "tampering detected" in str(exc_info.value).lower()
    )


def test_corrupted_object_raises_integrity_error(test_env):
    """Corrupting a blob object in the store raises SnapshotIntegrityError on restore."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "payload.txt").write_text("precious payload\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    snap_files = manager.list_snapshot_files(snap_sha)
    blob_sha = snap_files["payload.txt"]

    obj_path = store / "objects" / blob_sha[:2] / blob_sha[2:]
    assert obj_path.exists()
    os.chmod(obj_path, stat.S_IWRITE)
    obj_path.write_bytes(b"corrupted garbage bytes that break git decompression")

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert "corrupt or unreadable" in str(exc_info.value).lower()


# ==============================================================================
# 6. Junction Traversal & Non-Descending Cleanup Protection
# ==============================================================================

def test_symlink_traversal_neutralized(test_env):
    """Planted symlinks pointing outside workspace are neutralized before restore."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "clean.txt").write_text("clean\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    outside_target = ws.parent / "sensitive_outside.txt"
    outside_target.write_text("do not overwrite\n", encoding="utf-8")

    link_path = ws / "escape_link"
    try:
        link_path.symlink_to(outside_target)
    except (OSError, NotImplementedError):
        # Explicit status report: Developer Mode / SeCreateSymbolicLinkPrivilege absent
        check_strict_skip("Developer Mode / SeCreateSymbolicLinkPrivilege absent (WinError 5)")

    assert link_path.is_symlink()

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)
    assert not link_path.exists(), "Symlink was not neutralized before restore!"
    assert outside_target.read_text(encoding="utf-8") == "do not overwrite\n"


def test_cleanup_does_not_descend_through_junction(test_env):
    """Untracked-file cleanup and restore do NOT descend through a planted junction."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "main.py").write_text("main()\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # 1. Outside directory containing valuable files
    outside_dir = ws.parent / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("vital data do not delete\n", encoding="utf-8")

    # 2. Plant junction in workspace pointing to outside_dir
    junction_path = ws / "escape_junction"
    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir.resolve()), str(junction_path.resolve()))
            junction_created = True
        except (OSError, NotImplementedError):
            res = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction_path), str(outside_dir)],
                capture_output=True,
            )
            junction_created = (res.returncode == 0)

    if not junction_created:
        check_strict_skip("OS environment prevented junction creation")

    assert junction_path.exists()

    # 3. Add an untracked file inside workspace
    (ws / "stray.py").write_text("stray\n", encoding="utf-8")

    # 4. Restore turn snapshot
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # 5. Assert: junction is removed, workspace untracked file is removed
    assert not junction_path.exists(), "Junction was not removed during restore!"
    assert not (ws / "stray.py").exists(), "Untracked file was not removed!"

    # 6. Critical assertion: outside directory and outside file are completely intact!
    assert outside_dir.exists(), "Cleanup erroneously removed outside target directory!"
    assert outside_file.exists(), "Cleanup recursively descended through junction and deleted outside files!"
    assert outside_file.read_text(encoding="utf-8") == "vital data do not delete\n"


def test_replace_tracked_dir_with_junction_restores_cleanly(test_env):
    """Replace a tracked directory with a junction to an outside folder;
    revert removes the junction first, restores a real dir, and writes nothing outside."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    # 1. Pre-turn: create tracked directory with file
    tracked_dir = ws / "tracked_dir"
    tracked_dir.mkdir()
    (tracked_dir / "file.txt").write_text("tracked content\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # 2. Outside directory with target files
    outside_dir = ws.parent / "outside_sensitive_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "target.txt"
    outside_file.write_text("untouchable payload\n", encoding="utf-8")

    # 3. Agent deletes tracked_dir and replaces it with junction pointing to outside_dir
    (tracked_dir / "file.txt").unlink()
    tracked_dir.rmdir()

    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir.resolve()), str(tracked_dir.resolve()))
            junction_created = True
        except (OSError, NotImplementedError):
            res = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(tracked_dir), str(outside_dir)],
                capture_output=True,
            )
            junction_created = (res.returncode == 0)

    if not junction_created:
        check_strict_skip("OS environment prevented junction creation")

    # Verify tracked_dir is now a reparse point
    st = os.lstat(str(tracked_dir))
    assert bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    # 4. Restore turn snapshot
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # 5. Assert: junction is removed and replaced with a genuine directory
    assert tracked_dir.exists()
    st_after = os.lstat(str(tracked_dir))
    assert not bool(st_after.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT), "Tracked dir remained a junction!"
    assert (tracked_dir / "file.txt").exists()
    assert (tracked_dir / "file.txt").read_text(encoding="utf-8") == "tracked content\n"

    # 6. Outside target directory is completely intact
    assert outside_file.exists()
    assert outside_file.read_text(encoding="utf-8") == "untouchable payload\n"


def test_gitignore_edit_cannot_hide_files(test_env):
    """Edits to workspace .gitignore cannot hide files from turn snapshot."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "app.py").write_text("print('app')\n", encoding="utf-8")
    (ws / ".gitignore").write_text("evil.py\n", encoding="utf-8")
    (ws / "evil.py").write_text("print('evil')\n", encoding="utf-8")

    snap_sha = manager.create_turn_snapshot("turn-1")
    snap_files = manager.list_snapshot_files(snap_sha)
    assert "evil.py" in snap_files, "evil.py was hidden from snapshot by .gitignore!"
    assert "app.py" in snap_files


def test_rename_delete_restore_cycle(test_env):
    """Restores full state when files are renamed, deleted, or altered."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "keep.txt").write_text("keep v1\n", encoding="utf-8")
    (ws / "delete_me.txt").write_text("delete v1\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

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
# 7. Post-Turn Nested .git Check & Reaping
# ==============================================================================

def test_post_turn_nested_git_reaps_before_raising(test_env):
    """Post-turn nested .git check ensures processes are reaped before raising NestedGitError."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "app.py").write_text("app()\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # Spawn background looping process
    proc = subprocess.Popen([sys.executable, "-c", "import time; \nwhile True: time.sleep(0.05)"])
    reaper.register_process(proc)

    # Agent plants nested git during the turn
    evil_dir = ws / "nested_repo"
    evil_dir.mkdir()
    subprocess.run(["git", "init"], cwd=evil_dir, check=True, capture_output=True, env=git_env)

    # Post-turn check catches nested git, reaps process and restores before raising
    with pytest.raises(NestedGitError):
        manager.check_post_turn("turn-1")

    # Process must be dead
    time.sleep(0.2)
    assert proc.poll() is not None, "Background process was not reaped when NestedGitError was raised!"
    # Workspace must be restored
    assert not evil_dir.exists(), "Planted nested git was not cleaned up on post-turn failure!"
    assert (ws / "app.py").read_text(encoding="utf-8") == "app()\n"


# ==============================================================================
# 8. Non-Default NTFS Alternate Data Streams (ADS) Eradication
# ==============================================================================

def test_non_default_ads_wiped_on_unchanged_file(test_env):
    """Non-default ADS added to an unchanged file is detected and eradicated."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    target_file = ws / "unchanged.txt"
    target_file.write_text("clean content\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    # During turn, agent attaches ADS stream to unchanged file
    ads_file = ws / "unchanged.txt:hidden_payload"
    ads_supported = False
    try:
        ads_file.write_text("stealth exploit", encoding="utf-8")
        ads_supported = True
    except (OSError, NotImplementedError):
        pass

    if not ads_supported:
        check_strict_skip("NTFS ADS not supported on this filesystem")

    assert ads_file.read_text(encoding="utf-8") == "stealth exploit"

    # Restore turn snapshot
    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    # Primary content intact
    assert target_file.read_text(encoding="utf-8") == "clean content\n"

    # Non-default ADS stream must be completely eradicated
    assert not ads_file.exists(), "Non-default ADS persisted on unchanged file across restore!"


# ==============================================================================
# 9. Workspace .git Index and HEAD Integrity Across Restores
# ==============================================================================

def test_workspace_git_index_and_head_untouched_by_revert(test_env):
    """Reverting a turn does not touch workspace HEAD, branch refs, or .git/index."""
    ws, store, manager, initial_head_sha, git_env, reaper = test_env

    index_path = ws / ".git" / "index"
    initial_index_bytes = index_path.read_bytes()

    (ws / "feature.py").write_text("v1\n", encoding="utf-8")
    snap_sha = manager.create_turn_snapshot("turn-1")

    (ws / "feature.py").write_text("v2\n", encoding="utf-8")
    (ws / "stray.py").write_text("stray\n", encoding="utf-8")

    manager.restore_turn_snapshot("turn-1", expected_sha=snap_sha)

    assert (ws / "feature.py").read_text(encoding="utf-8") == "v1\n"
    assert not (ws / "stray.py").exists()

    curr_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()
    assert curr_head == initial_head_sha, "Workspace HEAD was moved during turn revert!"
    assert index_path.read_bytes() == initial_index_bytes, "Workspace .git/index was modified by revert!"


# ==============================================================================
# 10. Rescue Ref Preservation on Abort
# ==============================================================================

def test_rescue_ref_preserved_on_abort(test_env):
    """Aborted runs preserve their workspace state under refs/stackmind/rescue/<run_id>."""
    ws, store, manager, head_sha, git_env, reaper = test_env

    (ws / "work_in_progress.py").write_text("half_finished()\n", encoding="utf-8")
    rescue_sha = manager.create_rescue_ref("run-abort-99")

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

    files = manager.list_snapshot_files(rescue_sha)
    assert "work_in_progress.py" in files
