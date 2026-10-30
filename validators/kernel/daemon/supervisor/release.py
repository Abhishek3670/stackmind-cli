"""Release-commit provenance and work-order archiving helpers.

Module-level helpers used by the GITOPS phase; re-exported from the package root.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("stackmind.supervisor")

def format_release_commit_message(
    summary: str,
    work_order_id: str,
    released_by: str = "local-llm",
    approved_by: str = "gemma",
    architect: str = "claude",
    target_work_orders: list[str] | None = None,
) -> str:
    """Format a release commit message with standard provenance trailers.

    Conforms to Git trailer conventions:
    Work-Order: <id>
    Released-By: <agent>
    Approved-By: <agent>
    Architect: <agent>
    Target-Work-Orders: <ids>
    """
    clean_summary = summary.strip()
    trailers = [
        f"Work-Order: {work_order_id}",
        f"Released-By: {released_by}",
        f"Approved-By: {approved_by}",
        f"Architect: {architect}",
    ]
    if target_work_orders:
        trailers.append(f"Target-Work-Orders: {', '.join(target_work_orders)}")
    return f"{clean_summary}\n\n" + "\n".join(trailers) + "\n"

def create_gitops_commit(
    workspace: Path | str,
    work_order_id: str,
    summary: str = "feat(release): product release",
    released_by: str = "local-llm",
    approved_by: str = "gemma",
    architect: str = "claude",
    target_work_orders: list[str] | None = None,
) -> str:
    """Create a governed GitOps release commit with standard provenance trailers.

    Stages files adhering to .gitignore and commits with trailers.
    Returns the created commit SHA.
    """
    ws = Path(workspace).resolve()
    commit_msg = format_release_commit_message(
        summary=summary,
        work_order_id=work_order_id,
        released_by=released_by,
        approved_by=approved_by,
        architect=architect,
        target_work_orders=target_work_orders,
    )
    subprocess.run(["git", "add", "."], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha

def archive_completed_work_orders(
    workspace: Path | str,
    completed_wo_ids: list[str] | None = None,
) -> list[str]:
    """Archive completed work orders from ACTIVE to COMPLETED and update INDEX.yaml/TREE.yaml.

    Returns the list of work order IDs that were successfully archived.
    """
    ws = Path(workspace).resolve()
    sync_dir = ws / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    completed_dir = sync_dir / "work-orders" / "COMPLETED"
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    tree_file = sync_dir / "runtime" / "TREE.yaml"

    if not active_dir.is_dir():
        return []

    completed_dir.mkdir(parents=True, exist_ok=True)
    target_ids = set(completed_wo_ids or [])

    archived: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for wo_file in sorted(active_dir.glob("*.yaml")):
        wo_id = wo_file.stem
        try:
            wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8")) or {}
        except Exception:
            wo_data = {}

        is_completed = (
            wo_id in target_ids
            or str(wo_data.get("status", "")).upper() == "COMPLETED"
        )
        if not is_completed:
            continue

        wo_data["status"] = "COMPLETED"
        wo_data["updated"] = now_iso
        # Bind this completion to the revision that actually ran, and keep the
        # amendment that invalidated earlier evidence visible in the record
        # instead of letting a stale verification silently look current.
        stale = wo_data.get("evidence_stale")
        if isinstance(stale, dict) and stale:
            completion = wo_data.get("completion")
            if not isinstance(completion, dict):
                completion = {}
            completion["prior_evidence_invalidated"] = stale
            completion["verified_at_revision"] = wo_data.get("revision")
            contract_file = sync_dir / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    contract_data = yaml.safe_load(
                        contract_file.read_text(encoding="utf-8")
                    ) or {}
                    completion["verified_at_contract_revision"] = (
                        contract_data.get("contract_revision")
                        if isinstance(contract_data, dict) else None
                    )
                except Exception:
                    completion["verified_at_contract_revision"] = None
            completion["reverified_at"] = now_iso
            wo_data["completion"] = completion
            from validators.kernel.work_order_ops import (
                stale_requires_rework,
                stale_sources_resolved,
            )

            resolved, unresolved_reason = stale_sources_resolved(ws, stale)
            if resolved and stale_requires_rework(stale):
                # A rework-class invalidation (acceptance criteria, description,
                # deliverable, scope) is not satisfied by re-running the old
                # checks: it needs a lifecycle reopen. Archiving must not clear
                # it, or release would accept evidence the marker already
                # rejected.
                wo_data["evidence_stale"] = {
                    **stale,
                    "revalidation_blocked_reason": (
                        "invalidation may require code changes; reopen the work "
                        "order and complete the required rework"
                    ),
                }
            elif resolved:
                # Evidence is fresh for this revision again; the invalidation
                # stays visible in the completion record and the durable audit.
                wo_data.pop("evidence_stale", None)
            else:
                # An upstream advanced again while this work order was in
                # flight. Archiving must not bless evidence that no longer
                # applies: the marker survives, so release stays blocked until
                # the dependent is re-verified (or reopened) against it.
                wo_data["evidence_stale"] = {
                    **stale,
                    "revalidation_blocked_reason": unresolved_reason,
                }
        target_file = completed_dir / wo_file.name
        from validators.kernel.io import durable_write_yaml
        durable_write_yaml(target_file, wo_data)
        try:
            wo_file.unlink()
        except OSError:
            pass
        archived.append(wo_id)

    # Also include any target_ids that are already in completed_dir
    for target_id in target_ids:
        if (completed_dir / f"{target_id}.yaml").is_file() and target_id not in archived:
            archived.append(target_id)

    if not archived:
        return []

    # Update INDEX.yaml
    index_data: dict[str, Any] = {}
    if index_file.is_file():
        try:
            index_data = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
            orders = index_data.get("orders", [])
            for o in orders:
                if isinstance(o, dict) and o.get("id") in archived:
                    o["status"] = "COMPLETED"
                    o["file"] = f"work-orders/COMPLETED/{o.get('id')}.yaml"
                    o["updated"] = now_iso
            index_data["total_active"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
            )
            index_data["total_completed"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
            )
            index_data["total_blocked"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
            )
            index_data["last_updated"] = now_iso
            durable_write_yaml(index_file, index_data)
        except Exception:
            pass

    # Update TREE.yaml
    try:
        from cli.init import ensure_canonical_tree
        tree_data = ensure_canonical_tree(ws)
        if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
            if index_file.is_file():
                tree_data["work_orders"]["total_active"] = index_data.get("total_active", 0)
                tree_data["work_orders"]["total_completed"] = index_data.get("total_completed", 0)
                tree_data["work_orders"]["total_blocked"] = index_data.get("total_blocked", 0)
        if "agents" in tree_data and isinstance(tree_data["agents"], dict):
            archived_set = set(archived)
            for _agent_name, agent_info in tree_data["agents"].items():
                if isinstance(agent_info, dict) and "assigned_work_orders" in agent_info:
                    assigned = agent_info["assigned_work_orders"]
                    if isinstance(assigned, list):
                        agent_info["assigned_work_orders"] = [
                            w for w in assigned if w not in archived_set
                        ]
        tree_data["last_updated"] = now_iso
        durable_write_yaml(tree_file, tree_data)
    except Exception:
        pass

    try:
        from validators.harness.runner import HarnessRunner
        HarnessRunner._sync_index_and_tree_yaml(ws)
    except Exception:
        pass

    if archived:
        try:
            from validators.kernel.sync_git import commit_sync_governance
            commit_sync_governance(
                ws,
                f"chore(governance): archive completed work orders: {', '.join(archived)}",
                author="supervisor",
            )
        except Exception:
            pass

    return archived

def archive_blocked_work_orders(
    workspace: Path | str,
    blocked_wo_ids: list[str] | None = None,
) -> list[str]:
    """Archive blocked/failed/terminal work orders from ACTIVE to BLOCKED and update INDEX.yaml/TREE.yaml.

    Returns the list of work order IDs that were successfully archived.
    """
    ws = Path(workspace).resolve()
    sync_dir = ws / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    blocked_dir = sync_dir / "work-orders" / "BLOCKED"
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    tree_file = sync_dir / "runtime" / "TREE.yaml"

    if not active_dir.is_dir():
        return []

    blocked_dir.mkdir(parents=True, exist_ok=True)
    target_ids = set(blocked_wo_ids or [])

    archived: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    archived_status_map: dict[str, str] = {}
    for wo_file in sorted(active_dir.glob("*.yaml")):
        wo_id = wo_file.stem
        try:
            wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8")) or {}
        except Exception:
            wo_data = {}

        st = str(wo_data.get("status", "")).upper()
        is_blocked = (
            wo_id in target_ids
            or st in ("BLOCKED", "FAILED", "CANCELLED")
        )
        if not is_blocked:
            continue

        if not wo_data.get("status") or str(wo_data.get("status", "")).upper() == "ACTIVE":
            wo_data["status"] = "BLOCKED"
        archived_status_map[wo_id] = str(wo_data.get("status") or "BLOCKED").upper()
        wo_data["updated"] = now_iso
        target_file = blocked_dir / wo_file.name
        from validators.kernel.io import durable_write_yaml
        durable_write_yaml(target_file, wo_data)
        try:
            wo_file.unlink()
        except OSError:
            pass
        archived.append(wo_id)

    for target_id in target_ids:
        b_target = blocked_dir / f"{target_id}.yaml"
        if b_target.is_file():
            if target_id not in archived:
                archived.append(target_id)
            if target_id not in archived_status_map:
                try:
                    b_data = yaml.safe_load(b_target.read_text(encoding="utf-8")) or {}
                    archived_status_map[target_id] = str(b_data.get("status") or "BLOCKED").upper()
                except Exception:
                    archived_status_map[target_id] = "BLOCKED"

    if not archived:
        return []

    # Update INDEX.yaml
    index_data: dict[str, Any] = {}
    if index_file.is_file():
        try:
            index_data = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
            orders = index_data.get("orders", [])
            for o in orders:
                if isinstance(o, dict) and o.get("id") in archived:
                    o["status"] = archived_status_map.get(o.get("id"), "BLOCKED")
                    o["file"] = f"work-orders/BLOCKED/{o.get('id')}.yaml"
                    o["updated"] = now_iso
            index_data["total_active"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
            )
            index_data["total_completed"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
            )
            index_data["total_blocked"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
            )
            index_data["last_updated"] = now_iso
            durable_write_yaml(index_file, index_data)
        except Exception:
            pass

    # Update TREE.yaml
    try:
        from cli.init import ensure_canonical_tree
        tree_data = ensure_canonical_tree(ws)
        if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
            if index_file.is_file():
                tree_data["work_orders"]["total_active"] = index_data.get("total_active", 0)
                tree_data["work_orders"]["total_completed"] = index_data.get("total_completed", 0)
                tree_data["work_orders"]["total_blocked"] = index_data.get("total_blocked", 0)
        if "agents" in tree_data and isinstance(tree_data["agents"], dict):
            archived_set = set(archived)
            for _agent_name, agent_info in tree_data["agents"].items():
                if isinstance(agent_info, dict) and "assigned_work_orders" in agent_info:
                    assigned = agent_info["assigned_work_orders"]
                    if isinstance(assigned, list):
                        agent_info["assigned_work_orders"] = [
                            w for w in assigned if w not in archived_set
                        ]
        tree_data["last_updated"] = now_iso
        durable_write_yaml(tree_file, tree_data)
    except Exception:
        pass

    try:
        from validators.harness.runner import HarnessRunner
        HarnessRunner._sync_index_and_tree_yaml(ws)
    except Exception:
        pass

    return archived
