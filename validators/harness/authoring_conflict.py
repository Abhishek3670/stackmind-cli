"""Overwrite conflict detection for authored artifacts.\n\nPrevents clobbering existing active work orders or contracts from other tasks.\nTerminal buckets (COMPLETED, BLOCKED) are protected from in-place rewriting.\n"""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any, Set

# Track paths authored in the current session (shared state)
_session_authored_paths: Set[str] = set()


def _check_overwrite_conflict(
    norm_path: str,
    content: str,
    data: Any,
    project_root: Path,
    errors: list[str],
    author: str | None = None,
) -> None:
    """Prevent overwriting existing active work orders or contracts from other tasks."""

    is_active_wo = (
        norm_path.startswith(".sync/work-orders/ACTIVE/")
        or norm_path.startswith("work-orders/ACTIVE/")
    )
    is_contract = (
        norm_path.startswith(".sync/contracts/")
        or norm_path.startswith("contracts/")
    )
    is_terminal_wo = any(
        norm_path.startswith(prefix)
        for prefix in (
            ".sync/work-orders/COMPLETED/",
            "work-orders/COMPLETED/",
            ".sync/work-orders/BLOCKED/",
            "work-orders/BLOCKED/",
        )
    )

    if not (is_active_wo or is_contract or is_terminal_wo):
        return

    # Check candidate locations in project root
    candidates = [project_root / norm_path]
    if not norm_path.startswith(".sync/"):
        candidates.append(project_root / ".sync" / norm_path)

    existing_file = next((p for p in candidates if p.is_file()), None)
    if existing_file is None:
        return

    # If already authored by THIS session, allow updating it (never for a
    # terminal work order, whose content is completion evidence).
    if norm_path in _session_authored_paths and not is_terminal_wo:
        return

    # Check if existing content matches what this session is authoring
    try:
        existing_content = existing_file.read_text(encoding="utf-8")
    except Exception:
        existing_content = ""

    # Exact textual match (idempotent write)
    if existing_content.strip() == content.strip():
        return

    # Semantic YAML dictionary match
    try:
        existing_data = yaml.safe_load(existing_content)
    except Exception:
        existing_data = None

    if isinstance(existing_data, dict) and isinstance(data, dict) and existing_data == data:
        return

    if is_terminal_wo:
        errors.append(
            f"Overwrite conflict: terminal work order '{norm_path}' cannot be rewritten "
            "in place. Reopen or supersede it through the lifecycle so its completion "
            "evidence is explicitly invalidated."
        )
        return

    # Scaffolding or repair artifacts: an authorized author (Architect/CEO)\n    # may supersede synthesized artifacts or amend contracts/work orders.\n    # Workers never reach this check (role gate runs first).
    author_lower = (author or "").lower()
    if author_lower in {"claude", "architecture", "architect", "ceo", "admin", "system", "test"} and isinstance(existing_data, dict):
        # The Architect has full authority to cancel, split, amend, or supersede work orders to maintain flow
        status_val = str(data.get("status", "") if isinstance(data, dict) else "").upper()
        if status_val == "CANCELLED":
            return
        if status_val == "SUPERSEDED" or (isinstance(data, dict) and bool(data.get("superseded_by"))):
            return
        existing_synth = str(existing_data.get("synthesized_by", "")).lower()
        new_synth = str(data.get("synthesized_by", "") if isinstance(data, dict) else "").lower()
        repair_markers = {
            "bootstrap",
            "recovery-repair",
            "architect-repair",
            "authoring-repair",
            "repair",
            "reconcile",
        }
        is_repair = (
            existing_synth in repair_markers
            or new_synth in repair_markers
            or str(existing_data.get("status", "")).upper() in {"BLOCKED", "REWORK"}
            or bool(existing_data.get("error") or existing_data.get("blocked_reason"))
        )
        if not is_repair and is_contract:
            wo_stem = Path(norm_path).stem
            wo_cand = project_root / ".sync" / "work-orders" / "ACTIVE" / f"{wo_stem}.yaml"
            if not wo_cand.exists():
                wo_cand = project_root / "work-orders" / "ACTIVE" / f"{wo_stem}.yaml"
            if wo_cand.is_file():
                try:
                    wo_data = yaml.safe_load(wo_cand.read_text(encoding="utf-8"))
                    if isinstance(wo_data, dict):
                        if (
                            str(wo_data.get("status", "")).upper() in {"BLOCKED", "REWORK"}
                            or bool(wo_data.get("error") or wo_data.get("blocked_reason"))
                        ):
                            is_repair = True
                except Exception:
                    pass

        if not is_repair:
            sup_dir = project_root / ".sync" / "runtime" / "supervisor"
            if not sup_dir.exists():
                sup_dir = project_root / "runtime" / "supervisor"
            if sup_dir.is_dir():
                for rf in sup_dir.glob("run-*.yaml"):
                    try:
                        rdata = yaml.safe_load(rf.read_text(encoding="utf-8"))
                        if isinstance(rdata, dict) and (
                            str(rdata.get("phase", "")).upper() in {"ARCHITECT_REPAIR", "BLOCKED", "AUTHORING_READINESS_GATE"}
                            or bool(rdata.get("readiness_issues"))
                        ):
                            is_repair = True
                            break
                    except Exception:
                        pass

        if is_repair:
            # During repair of blocked/failing/scaffold artifacts or active supervisor runs,
            # an authorized author is explicitly authorized to replace or refine the artifact.
            if is_active_wo and isinstance(data, dict):
                existing_rev = existing_data.get("revision", 1)
                new_rev = data.get("revision")
                if not (isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev):
                    existing_num = int(existing_rev) if isinstance(existing_rev, (int, float)) else 1
                    bumped_rev = max(existing_num + 1, int(new_rev or 0) + 1)
                    data["revision"] = bumped_rev
                    amendments = data.setdefault("amendments", [])
                    if isinstance(amendments, list):
                        amendments.append(
                            {
                                "revision": bumped_rev,
                                "reason": "Authoring refinement and governance amendment",
                            }
                        )
            elif is_contract and isinstance(data, dict):
                existing_rev = existing_data.get("contract_revision", 1)
                new_rev = data.get("contract_revision")
                existing_num = int(existing_rev) if isinstance(existing_rev, (int, float)) else 1
                if isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev:
                    data["contract_revision"] = max(existing_num + 1, int(new_rev or 0))
            return

        if is_contract and isinstance(data, dict):
            existing_rev = existing_data.get("contract_revision", 1)
            new_rev = data.get("contract_revision")
            if isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev:
                return

        # Task amendment: an authorized author may sharpen a live work
        # order in place, but only behind a strictly increasing revision
        # (recorded with its amendment reason), never a silent clobber.
        if is_active_wo and isinstance(data, dict):
            existing_rev = existing_data.get("revision", 1)
            new_rev = data.get("revision")
            if isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev:
                return

    # Conflict: attempting to overwrite an active artifact from another task or unauthorized update
    artifact_label = "work order" if is_active_wo else "contract"
    errors.append(
        f"Overwrite conflict: active {artifact_label} '{norm_path}' already exists on disk "
        f"with different content. Overwriting an existing active task is rejected to prevent "
        f"silently clobbering live work orders."
    )