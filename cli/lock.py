"""stackmind lock — Write-lock mechanism for serialized canonical writes.

Implements PLAT-03: a single ``.sync/runtime/LOCK`` marker that an agent
acquires when it begins mutating canonical paths (``runtime/boot/``,
``runtime/TREE.yaml``) and releases when its session ends.

The lock is advisory at the tooling layer: the CLI cannot physically prevent
an editor from writing a file, but it gives every agent (and CI) a verifiable
way to detect that another agent holds the write lock and refuse to proceed.
``stackmind shutdown`` is the canonical mechanism that clears the lock.

Lock file format (YAML):

    held_by: claude
    session_id: 30
    acquired_at: 2026-06-10T09:14:00+05:30
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml

LOCK_REL = "runtime/LOCK"


def get_lock_path(sync_path: Path) -> Path:
    """Return the path to the LOCK file for a given .sync/ directory."""
    return sync_path / "runtime" / "LOCK"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_lock(sync_path: Path) -> dict | None:
    """Read and parse the LOCK file.

    Returns:
        The parsed lock mapping if a well-formed lock exists, otherwise None
        (both when no lock is present and when the lock is malformed). Use
        :func:`lock_is_malformed` to distinguish those two cases.
    """
    lock_path = get_lock_path(sync_path)
    if not lock_path.exists():
        return None
    try:
        data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict) or "held_by" not in data:
        return None
    return data


def lock_is_malformed(sync_path: Path) -> bool:
    """Return True if a LOCK file exists but is not a well-formed lock."""
    lock_path = get_lock_path(sync_path)
    if not lock_path.exists():
        return False
    return read_lock(sync_path) is None


def acquire_lock(
    sync_path: Path,
    agent: str,
    session_id: str | int | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Acquire the write lock for ``agent`` with atomic creation to prevent TOCTOU races.

    Args:
        sync_path: Path to the .sync/ runtime directory.
        agent: Name of the agent acquiring the lock.
        session_id: Optional session identifier recorded in the lock.
        force: If True, steal the lock even if another agent holds it.

    Returns:
        (success, message). Acquisition fails when another agent already holds
        the lock and ``force`` is not set. Re-acquiring a lock already held by
        the same agent succeeds and refreshes the timestamp.
    """
    import os
    runtime_dir = sync_path / "runtime"
    if not runtime_dir.is_dir():
        return False, f"runtime directory not found at {runtime_dir}"

    lock_path = get_lock_path(sync_path)
    lock_data: dict = {
        "held_by": agent,
        "session_id": session_id,
        "acquired_at": _now_iso(),
    }
    payload = yaml.dump(lock_data, default_flow_style=False, sort_keys=False).encode("utf-8")

    # Atomic creation attempt (O_CREAT | O_EXCL)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return True, f"LOCK acquired by '{agent}'"
    except FileExistsError:
        pass  # Lock file already exists; inspect it

    existing = read_lock(sync_path)
    stolen = False
    previous_holder = None
    if existing is not None:
        holder = existing.get("held_by")
        if holder != agent:
            if not force:
                return (
                    False,
                    f"LOCK held by '{holder}' (session {existing.get('session_id')}, "
                    f"acquired {existing.get('acquired_at')}). "
                    f"Use --force to override or wait for '{holder}' to shut down.",
                )
            else:
                stolen = True
                previous_holder = holder

    # Update/steal existing lock atomically via temporary file and atomic replace
    tmp_path = lock_path.with_suffix(".tmp")
    tmp_path.write_bytes(payload)
    try:
        os.replace(tmp_path, lock_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    if stolen:
        receipts_dir = sync_path / "runtime" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).isoformat()
        filename_timestamp = timestamp_str.replace(":", "-").replace(".", "-")
        event_file = receipts_dir / f"LOCK_STOLEN_{agent}_{filename_timestamp}.yaml"
        event_data = {
            "event_type": "LOCK_STOLEN",
            "agent": agent,
            "session_id": session_id,
            "stolen_from": previous_holder,
            "timestamp": timestamp_str,
        }
        event_file.write_text(
            yaml.dump(event_data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return True, f"LOCK forcibly acquired by '{agent}' (was held by '{previous_holder}')"
    return True, f"LOCK acquired by '{agent}'"


def release_lock(sync_path: Path, agent: str, force: bool = False) -> tuple[bool, str]:
    """Release the write lock held by ``agent``.

    Args:
        sync_path: Path to the .sync/ runtime directory.
        agent: Name of the agent releasing the lock.
        force: If True, release even when another agent holds the lock.

    Returns:
        (success, message). Releasing when no lock exists is a no-op success
        (idempotent). Releasing a lock held by a different agent fails unless
        ``force`` is set.
    """
    lock_path = get_lock_path(sync_path)
    existing = read_lock(sync_path)

    if existing is None:
        # No well-formed lock. If a malformed file exists, clean it up.
        if lock_path.exists():
            lock_path.unlink()
            return True, "Removed malformed LOCK file"
        return True, "No LOCK held; nothing to release"

    holder = existing.get("held_by")
    if holder != agent and not force:
        return (
            False,
            f"LOCK held by '{holder}', not '{agent}'. Use --force to override.",
        )

    lock_path.unlink(missing_ok=True)
    if holder != agent:
        return True, f"LOCK forcibly released (was held by '{holder}')"
    return True, f"LOCK released by '{agent}'"


def lock_held_by_other(sync_path: Path, agent: str) -> bool:
    """Return True if a valid lock is held by an agent other than ``agent``."""
    existing = read_lock(sync_path)
    return existing is not None and existing.get("held_by") != agent
