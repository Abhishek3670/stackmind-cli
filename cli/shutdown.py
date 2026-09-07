"""stackmind shutdown — Agent shutdown with handoff validation.

Ensures agents properly hand off work before ending their session.
Validates handoff report exists, updates state, and archives session.
"""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


import yaml
from rich.console import Console

console = Console()


def _load_yaml(path: Path) -> dict | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _next_session_id(sync_path: Path, agent: str) -> int | None:
    """Return the next session number for an agent, if its boot snapshot exists."""
    boot_path = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
    data = _load_yaml(boot_path)
    if not isinstance(data, dict):
        return None
    return data.get("session_count", 0) + 1


def _git_head(project_path: Path) -> str | None:
    """Return the repo HEAD SHA, or None if git metadata is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return completed.stdout.strip() or None


def write_session_receipt(
    sync_path: Path,
    project_path: Path,
    agent: str,
    session_id: int | None,
    outcome: str,
    handoff_path: Path | None,
) -> Path | None:
    """Write a session-level shutdown receipt."""
    if session_id is None:
        return None

    timestamp = datetime.now(timezone.utc)
    receipt_timestamp = timestamp.strftime("%Y-%m-%dT%H%M%SZ")
    tree_version, _ = fresh_tree_versions(sync_path)
    receipts_dir = sync_path / "runtime" / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    receipt_data = {
        "agent": agent,
        "session_id": session_id,
        "timestamp": timestamp.isoformat(),
        "outcome": outcome,
        "handoff_path": (
            handoff_path.relative_to(sync_path).as_posix()
            if handoff_path is not None
            else None
        ),
        "commit_sha": _git_head(project_path),
        "tree_version": tree_version,
    }

    receipt_path = receipts_dir / f"{agent}-session-{session_id}-{receipt_timestamp}.yaml"
    _save_yaml(receipt_path, receipt_data)
    return receipt_path


def find_handoff_report(outbox_path: Path) -> Path | None:
    """Find the most recent handoff report in agent's outbox."""
    if not outbox_path.exists():
        return None
    
    handoffs = list(outbox_path.glob("handoff-*.md"))
    if not handoffs:
        return None
    
    # Return most recent by filename (timestamp-based)
    return sorted(handoffs, reverse=True)[0]


def unprocessed_inbox_items(sync_path: Path, agent: str) -> list[Path]:
    """Return inbox messages an agent has not yet processed.

    GEMMA-02: a message is considered processed once it has been moved into
    ``inbox/<agent>/_read/``. Any file remaining at the top level of
    ``inbox/<agent>/`` (excluding the ``_read/`` directory, the
    ``_deferred/`` directory, and ``.gitkeep``) is an unprocessed item.
    D024 requires each inbox item to have a documented outcome before a
    session closes, so these must be drained before shutdown.
    """
    inbox = sync_path / "inbox" / agent
    if not inbox.is_dir():
        return []

    items: list[Path] = []
    for entry in inbox.iterdir():
        if entry.is_dir():
            continue
        if entry.name == ".gitkeep":
            continue
        items.append(entry)
    return sorted(items)


def defer_inbox_items(sync_path: Path, agent: str, items: list[Path]) -> int:
    """Move unprocessed inbox items to _deferred/ and write receipt stubs.

    Each item is moved from ``inbox/<agent>/`` to ``inbox/<agent>/_deferred/``,
    and a receipt stub is written at
    ``runtime/receipts/<agent>_<item_name>.receipt.yaml`` with fields:
    ``deferred_by``, ``original_path``, ``deferred_at``, ``reason``.

    Args:
        sync_path: Path to the .sync/ directory.
        agent: Name of the agent whose items are being deferred.
        items: List of unprocessed inbox item paths to defer.

    Returns:
        Number of items deferred.
    """
    deferred_dir = sync_path / "inbox" / agent / "_deferred"
    receipts_dir = sync_path / "runtime" / "receipts"
    deferred_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for item in items:
        # Sanitize item name for receipt filename
        item_name = item.name
        receipt_name = f"{agent}_{item_name}.receipt.yaml"

        # Move item to _deferred/
        dest = deferred_dir / item_name
        shutil.move(str(item), str(dest))

        # Write receipt stub
        receipt_data = {
            "deferred_by": agent,
            "original_path": f"inbox/{agent}/{item_name}",
            "deferred_at": now_iso,
            "reason": "Deferred at shutdown via --defer flag",
        }
        _save_yaml(receipts_dir / receipt_name, receipt_data)
        count += 1

    return count


def archive_handoff(handoff_path: Path, outbox_path: Path) -> None:
    """Move handoff report to _read/ directory."""
    read_dir = outbox_path / "_read"
    read_dir.mkdir(parents=True, exist_ok=True)
    
    dest = read_dir / handoff_path.name
    shutil.move(str(handoff_path), str(dest))


def update_tree_status(sync_path: Path, agent: str) -> bool:
    """Update agent status to idle in TREE.yaml and sync work-order totals."""
    tree_path = sync_path / "runtime" / "TREE.yaml"
    if not tree_path.exists():
        return False
    
    data = _load_yaml(tree_path)
    if not data or "agents" not in data:
        return False
    
    if agent not in data["agents"]:
        return False
    
    data["agents"][agent]["status"] = "idle"
    data["agents"][agent]["last_task"] = "Session ended via shutdown"
    data["agents"][agent]["blockers"] = []
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Sync canonical work order totals from INDEX.yaml to prevent drift
    index_path = sync_path / "work-orders" / "INDEX.yaml"
    if index_path.exists():
        index_data = _load_yaml(index_path)
        if index_data and isinstance(data.get("work_orders"), dict):
            for field in ("total_active", "total_blocked", "total_completed"):
                if field in index_data:
                    data["work_orders"][field] = index_data[field]
    
    _save_yaml(tree_path, data)
    return True


def fresh_tree_versions(sync_path: Path) -> tuple[int | None, str | None]:
    """Re-read TREE.yaml at call time and return its current versions.

    CODEX-01: snapshot writes must use a fresh read of TREE.yaml taken
    immediately before the write — never a value cached at boot time. The
    boot-time PEEK of TREE is a read optimization only; using it as the source
    for a snapshot write captures a value that may have been superseded within
    the same session window (the canonical drift the boot sequence is meant to
    prevent).

    Returns:
        (tree_version, graph_version). Either may be None if TREE.yaml is
        missing/unreadable or the field is absent.
    """
    tree_path = sync_path / "runtime" / "TREE.yaml"
    if not tree_path.exists():
        return None, None
    data = _load_yaml(tree_path)
    if not isinstance(data, dict):
        return None, None
    return data.get("tree_version"), data.get("graph_version")


def update_boot_snapshot(sync_path: Path, agent: str) -> bool:
    """Persist an agent's boot snapshot at shutdown.

    Increments ``session_count`` and — per CODEX-01 — re-reads TREE.yaml fresh
    at write time to sync the snapshot's ``tree_version`` and ``graph_version``
    to the current canonical values. This keeps the snapshot from recording a
    stale, boot-cached version (CODEX-01) and prevents the snapshot from
    silently lagging TREE across sessions (GEMMA-01).
    """
    boot_path = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
    if not boot_path.exists():
        return False
    
    data = _load_yaml(boot_path)
    if not data:
        return False
    
    data["session_count"] = data.get("session_count", 0) + 1
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    # CODEX-01: fresh re-read of TREE.yaml immediately before writing the
    # snapshot — do not trust any value cached earlier in the session.
    tree_version, graph_version = fresh_tree_versions(sync_path)
    if tree_version is not None:
        data["tree_version"] = tree_version
    # graph_version syncs to current (including a deliberate null) only when
    # TREE.yaml was readable; preserve the existing value otherwise.
    if (sync_path / "runtime" / "TREE.yaml").exists():
        data["graph_version"] = graph_version
    
    _save_yaml(boot_path, data)
    return True


def shutdown(project_path: Path, agent: str, force: bool = False, defer: bool = False) -> bool:
    """Shutdown an agent session with handoff validation.

    Args:
        project_path: Root of the project containing .sync/.
        agent: Name of the agent to shutdown.
        force: If True, skip handoff validation (not recommended).
        defer: If True, defer unprocessed inbox items instead of blocking shutdown.

    Returns:
        True if shutdown successful, False otherwise.
    """
    sync_path = project_path / ".sync"

    if not sync_path.exists():
        console.print("[bold red][x] No .sync/ directory found[/bold red]")
        return False

    # Validate agent exists
    tree_path = sync_path / "runtime" / "TREE.yaml"
    tree_data = _load_yaml(tree_path)
    if not tree_data or agent not in tree_data.get("agents", {}):
        console.print(f"[bold red][x] Agent '{agent}' not found in TREE.yaml[/bold red]")
        return False

    outbox_path = sync_path / "outbox" / agent
    session_id = _next_session_id(sync_path, agent)

    def _write_error_receipt(handoff_path: Path | None = None) -> None:
        write_session_receipt(
            sync_path,
            project_path,
            agent,
            session_id,
            "error",
            handoff_path,
        )
    
    # Check for handoff report
    handoff = find_handoff_report(outbox_path)
    
    if not handoff and not force:
        console.print(f"[bold red][x] No handoff report found for '{agent}'[/bold red]")
        console.print(f"[dim]Expected: {outbox_path}/handoff-<timestamp>.md[/dim]")
        console.print("\n[yellow]Create a handoff report before shutdown to prevent lost work.[/yellow]")
        console.print("[dim]Use --force to skip this check (not recommended).[/dim]")
        _write_error_receipt()
        return False

    if handoff:
        # Pre-flight handoff protocol validation (GEMINI-02, LOCAL-LLM-01)
        if not force:
            from .validate import ValidationResult, _validate_handoff_file
            val_res = ValidationResult()
            _validate_handoff_file(handoff, agent, val_res, sync_path)
            if val_res.errors:
                console.print(f"[bold red][x] Handoff validation failed for '{handoff.name}':[/bold red]")
                for err in val_res.errors:
                    console.print(f"[dim]  - {err.message}[/dim]")
                console.print("\n[yellow]Fix handoff report issues before shutdown (or use --force).[/yellow]")
                _write_error_receipt(handoff)
                return False
        console.print(f"[green][+] Handoff report found: {handoff.name}[/green]")
    elif force:
        console.print("[yellow][!] Forcing shutdown without handoff report[/yellow]")

    # GEMMA-02: require a drained inbox before allowing the session to close.
    # Each inbox item must have a documented outcome (D024); silently leaving
    # unprocessed messages risks dropping required reviews or directives.
    pending = unprocessed_inbox_items(sync_path, agent)
    if pending and not force:
        if defer:
            deferred_count = defer_inbox_items(sync_path, agent, pending)
            console.print(
                f"[green][+] Deferred {deferred_count} unprocessed inbox item(s)[/green]"
            )
        else:
            console.print(
                f"[bold red][x] {len(pending)} unprocessed inbox item(s) for "
                f"'{agent}'[/bold red]"
            )
            for item in pending:
                console.print(f"[dim]  - inbox/{agent}/{item.name}[/dim]")
            console.print(
                "\n[yellow]Process each item (move to inbox/"
                f"{agent}/_read/ with a documented outcome) before shutdown.[/yellow]"
            )
            console.print("[dim]Use --force or --defer to handle this.[/dim]")
            _write_error_receipt(handoff)
            return False
    elif pending and force:
        console.print(
            f"[yellow][!] Forcing shutdown with {len(pending)} unprocessed "
            f"inbox item(s)[/yellow]"
        )

    # Update TREE.yaml
    if update_tree_status(sync_path, agent):
        console.print(f"[green][+] Updated TREE.yaml: {agent} -> idle[/green]")
    else:
        console.print(f"[bold red][x] Failed to update TREE.yaml[/bold red]")
        _write_error_receipt(handoff)
        return False

    # Update boot snapshot
    if update_boot_snapshot(sync_path, agent):
        console.print(f"[green][+] Incremented session_count in boot snapshot[/green]")
    else:
        console.print(f"[yellow][!] Could not update boot snapshot[/yellow]")

    # Archive handoff report
    archived_handoff: Path | None = None
    if handoff:
        archive_handoff(handoff, outbox_path)
        archived_handoff = outbox_path / "_read" / handoff.name
        console.print(f"[green][+] Archived handoff to _read/[/green]")

    session_outcome = "deferred" if pending and defer and not force else "clean"
    receipt_path = write_session_receipt(
        sync_path,
        project_path,
        agent,
        session_id,
        session_outcome,
        archived_handoff or handoff,
    )
    if receipt_path is not None:
        console.print(f"[green][+] Wrote session receipt: {receipt_path.name}[/green]")

    # Automatically record execution experience for Procedural Learning (LEARN-01)
    try:
        from validators.experience.recorder import ExperienceRecorder
        exp_rec = ExperienceRecorder.capture_from_session(
            project_path,
            agent,
            handoff_path=archived_handoff or handoff,
            session_id=session_id,
            save=True,
        )
        if exp_rec is not None:
            console.print(f"[green][+] Recorded procedural experience: {exp_rec.experience_id}[/green]")
    except Exception:
        # Experience capture failure must not block shutdown
        pass

    # Release the write lock (PLAT-03). Shutdown is the canonical mechanism
    # that clears the lock, enforcing serialized canonical writes across
    # sessions. Only the holding agent's lock is cleared in the normal flow;
    # if another agent holds it, surface a warning rather than stealing it.
    from .lock import read_lock, release_lock

    current_lock = read_lock(sync_path)
    if current_lock is not None and current_lock.get("held_by") not in (agent, None):
        if force:
            release_lock(sync_path, agent, force=True)
            console.print(
                f"[yellow][!] Force-released LOCK held by "
                f"'{current_lock.get('held_by')}'[/yellow]"
            )
        else:
            console.print(
                f"[yellow][!] LOCK held by '{current_lock.get('held_by')}', "
                f"not '{agent}' — leaving it in place "
                f"(use --force to override)[/yellow]"
            )
    else:
        released, _ = release_lock(sync_path, agent, force=force)
        if released and current_lock is not None:
            console.print(f"[green][+] Released write lock[/green]")

    # Auto-commit .sync governance receipts and state changes if .sync is a git repository
    if (sync_path / ".git").exists():
        try:
            subprocess.run(
                ["git", "add", "."],
                cwd=str(sync_path),
                capture_output=True,
                check=False,
            )
            commit_msg = f"chore(governance): agent {agent} session {session_id} shutdown"
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(sync_path),
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    console.print(f"\n[bold green]Agent '{agent}' shutdown complete.[/bold green]")
    return True
