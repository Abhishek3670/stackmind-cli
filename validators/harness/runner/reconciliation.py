"""Authored work-order reconciliation and index/tree sync.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

class _ReconciliationMixin:
    """Authored work-order reconciliation and index/tree sync."""

    def _reconcile_authored_work_orders(
        self,
        workspace_root: Path,
        decision: Any,
        auto_added_files: list[str],
    ) -> tuple[Any, list[str]]:
        """Reconcile architect-authored work orders against live closed work orders.

        When the architect authors child work orders starting from WO-001 on a
        continuation goal (where WO-001..WO-007 are already in COMPLETED/),
        remap the newly authored active work orders and contracts to the
        allocated or next sequential IDs to prevent multi-directory state conflicts.
        """
        closed_ids: set[str] = set()
        for sub in ("COMPLETED", "BLOCKED"):
            d = self.project_path / ".sync" / "work-orders" / sub
            if d.is_dir():
                closed_ids.update(f.stem for f in d.glob("*.yaml"))
        if not closed_ids:
            return decision, auto_added_files

        ws_active_dir = workspace_root / ".sync" / "work-orders" / "ACTIVE"
        ws_contracts_dir = workspace_root / ".sync" / "contracts"
        if not ws_active_dir.is_dir():
            return decision, auto_added_files

        active_files = sorted(ws_active_dir.glob("*.yaml"))
        if not active_files:
            return decision, auto_added_files

        active_ids = [f.stem for f in active_files]
        colliding_ids = [wo_id for wo_id in active_ids if wo_id in closed_ids]
        if not colliding_ids:
            return decision, auto_added_files

        proj_active_dir = self.project_path / ".sync" / "work-orders" / "ACTIVE"
        proj_active_ids = (
            sorted(f.stem for f in proj_active_dir.glob("*.yaml"))
            if proj_active_dir.is_dir()
            else []
        )
        proj_safe_ids = [wo_id for wo_id in proj_active_ids if wo_id not in closed_ids]

        if len(proj_safe_ids) >= len(active_files):
            target_ids = proj_safe_ids[: len(active_files)]
        else:
            from validators.kernel.daemon.authoring import get_next_work_order_int

            start_idx = get_next_work_order_int(self.project_path)
            target_ids = [f"WO-{start_idx + i:03d}" for i in range(len(active_files))]

        id_map: dict[str, str] = {}
        for old_id, new_id in zip(active_ids, target_ids):
            if old_id != new_id:
                id_map[old_id] = new_id

        if not id_map:
            return decision, auto_added_files

        # 1. Remap work order files in workspace_root
        for old_id, new_id in id_map.items():
            old_f = ws_active_dir / f"{old_id}.yaml"
            new_f = ws_active_dir / f"{new_id}.yaml"
            if old_f.is_file():
                try:
                    wo_data = yaml.safe_load(old_f.read_text(encoding="utf-8")) or {}
                    wo_data["id"] = new_id
                    deps = wo_data.get("dependencies", [])
                    if isinstance(deps, list):
                        wo_data["dependencies"] = [id_map.get(d, d) for d in deps]
                    new_f.write_text(yaml.safe_dump(wo_data, sort_keys=False), encoding="utf-8")
                    old_f.unlink(missing_ok=True)
                except Exception:
                    pass

        # 2. Remap contract files in workspace_root
        if ws_contracts_dir.is_dir():
            for old_id, new_id in id_map.items():
                old_c = ws_contracts_dir / f"{old_id}.yaml"
                new_c = ws_contracts_dir / f"{new_id}.yaml"
                if old_c.is_file():
                    try:
                        c_data = yaml.safe_load(old_c.read_text(encoding="utf-8")) or {}
                        c_data["work_order"] = new_id
                        new_c.write_text(yaml.safe_dump(c_data, sort_keys=False), encoding="utf-8")
                        old_c.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 3. Remap modified_files and auto_added_files
        def _remap_path_str(p: str) -> str:
            norm = p.replace("\\", "/")
            for old_id, new_id in id_map.items():
                old_wo = f"work-orders/ACTIVE/{old_id}.yaml"
                if norm.endswith(old_wo):
                    return norm.replace(f"{old_id}.yaml", f"{new_id}.yaml")
                old_c = f"contracts/{old_id}.yaml"
                if norm.endswith(old_c):
                    return norm.replace(f"{old_id}.yaml", f"{new_id}.yaml")
            return norm

        from dataclasses import replace

        if hasattr(decision, "modified_files") and decision.modified_files:
            remapped_mod = tuple(sorted({_remap_path_str(p) for p in decision.modified_files}))
            decision = replace(decision, modified_files=remapped_mod)

        auto_added_files = [_remap_path_str(p) for p in auto_added_files]
        return decision, auto_added_files

    @staticmethod
    def _reconcile_staged_work_order_collisions(staged_root: Path) -> None:
        """Ensure active work orders in staged_root do not collide with COMPLETED or BLOCKED states."""
        sync_dir = staged_root / ".sync"
        active_dir = sync_dir / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return
        closed_ids: set[str] = set()
        for sub in ("COMPLETED", "BLOCKED"):
            d = sync_dir / "work-orders" / sub
            if d.is_dir():
                closed_ids.update(f.stem for f in d.glob("*.yaml"))
        if not closed_ids:
            return

        contracts_dir = sync_dir / "contracts"
        for wo_file in list(active_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id in closed_ids:
                wo_file.unlink(missing_ok=True)
                if contracts_dir.is_dir():
                    (contracts_dir / f"{wo_id}.yaml").unlink(missing_ok=True)

    @staticmethod
    def _sync_index_and_tree_yaml(base_dir: Path) -> None:
        """Ensure .sync/work-orders/INDEX.yaml and .sync/runtime/TREE.yaml reflect all active work order files."""
        sync_dir = base_dir / ".sync"
        active_dir = sync_dir / "work-orders" / "ACTIVE"
        index_file = sync_dir / "work-orders" / "INDEX.yaml"
        if not active_dir.is_dir() and not (sync_dir / "work-orders").is_dir():
            return
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            index_data: dict[str, Any] = {}
            if index_file.is_file():
                try:
                    loaded = yaml.safe_load(index_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        index_data = loaded
                except Exception:
                    index_data = {}
            if not index_data:
                index_data = {
                    "schema_version": 1,
                    "last_updated": now_iso,
                    "updated_by": "stackmind-sync",
                    "orders": [],
                    "next_id": 1,
                    "total_active": 0,
                    "total_completed": 0,
                    "total_blocked": 0,
                }
                updated_index = True
            else:
                updated_index = False
            index_data.setdefault("next_id", 1)
            orders = index_data.setdefault("orders", [])
            existing_orders = {
                o.get("id"): o for o in orders if isinstance(o, dict) and "id" in o
            }
            assigned_per_agent: dict[str, list[str]] = {}

            completed_dir = sync_dir / "work-orders" / "COMPLETED"
            if completed_dir.is_dir():
                for comp_file in sorted(completed_dir.glob("*.yaml")):
                    comp_data = yaml.safe_load(comp_file.read_text(encoding="utf-8")) or {}
                    c_id = comp_data.get("id") or comp_file.stem
                    active_dup = active_dir / comp_file.name
                    if active_dup.is_file():
                        try:
                            active_dup.unlink()
                        except OSError:
                            pass
                    c_file = f"work-orders/COMPLETED/{comp_file.name}"
                    if c_id in existing_orders:
                        c_entry = existing_orders[c_id]
                        if c_entry.get("status") != "COMPLETED":
                            c_entry["status"] = "COMPLETED"
                            updated_index = True
                        if c_entry.get("file") != c_file:
                            c_entry["file"] = c_file
                            updated_index = True
                        if not c_entry.get("priority"):
                            c_entry["priority"] = comp_data.get("priority", "P0")
                            updated_index = True
                        if c_entry.get("dependencies") is None:
                            c_entry["dependencies"] = list(comp_data.get("dependencies") or [])
                            updated_index = True
                    else:
                        c_entry = {
                            "id": c_id,
                            "type": comp_data.get("type", "FEATURE"),
                            "title": comp_data.get("title", f"Work order {c_id}"),
                            "status": "COMPLETED",
                            "priority": comp_data.get("priority", "P0"),
                            "assigned_agents": comp_data.get("assigned_agents", ["codex"]),
                            "dependencies": comp_data.get("dependencies", []),
                            "created": str(comp_data.get("created") or now_iso),
                            "updated": str(comp_data.get("updated") or now_iso),
                            "file": c_file,
                        }
                        if comp_data.get("deliverable"):
                            c_entry["deliverable"] = comp_data["deliverable"]
                        orders.append(c_entry)
                        existing_orders[c_id] = c_entry
                        updated_index = True

            for wo_file in sorted(active_dir.glob("*.yaml")):
                wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if not isinstance(wo_data, dict):
                    continue
                wo_id = wo_data.get("id") or wo_file.stem
                agents = wo_data.get("assigned_agents", [])
                if isinstance(agents, list):
                    for ag in agents:
                        if isinstance(ag, str) and ag:
                            assigned_per_agent.setdefault(ag.lower().strip(), []).append(wo_id)
                elif isinstance(agents, str) and agents:
                    assigned_per_agent.setdefault(agents.lower().strip(), []).append(wo_id)

                if wo_id not in existing_orders:
                    created_val = wo_data.get("created") or now_iso
                    updated_val = wo_data.get("updated") or now_iso
                    new_entry = {
                        "id": wo_id,
                        "type": wo_data.get("type", "FEATURE"),
                        "title": wo_data.get("title", f"Work order {wo_id}"),
                        "status": wo_data.get("status", "ACTIVE"),
                        "priority": wo_data.get("priority", "P0"),
                        "assigned_agents": wo_data.get("assigned_agents", ["codex"]),
                        "dependencies": wo_data.get("dependencies", []),
                        "created": str(created_val),
                        "updated": str(updated_val),
                        "file": f"work-orders/ACTIVE/{wo_file.name}",
                    }
                    if wo_data.get("deliverable"):
                        new_entry["deliverable"] = wo_data["deliverable"]
                    orders.append(new_entry)
                    existing_orders[wo_id] = new_entry
                    updated_index = True
                else:
                    entry = existing_orders[wo_id]
                    target_status = wo_data.get("status", "ACTIVE")
                    target_file = f"work-orders/ACTIVE/{wo_file.name}"
                    if entry.get("status") != target_status:
                        entry["status"] = target_status
                        updated_index = True
                    if entry.get("file") != target_file:
                        entry["file"] = target_file
                        updated_index = True
                    if not entry.get("priority"):
                        entry["priority"] = wo_data.get("priority", "P1")
                        updated_index = True
                    if entry.get("dependencies") is None:
                        entry["dependencies"] = list(wo_data.get("dependencies") or [])
                        updated_index = True
                    if not entry.get("type") and wo_data.get("type"):
                        entry["type"] = wo_data["type"]
                        updated_index = True
                    if not entry.get("assigned_agents") and wo_data.get("assigned_agents"):
                        entry["assigned_agents"] = wo_data["assigned_agents"]
                        updated_index = True

            blocked_dir = sync_dir / "work-orders" / "BLOCKED"
            if blocked_dir.is_dir():
                for b_file in sorted(blocked_dir.glob("*.yaml")):
                    b_data = yaml.safe_load(b_file.read_text(encoding="utf-8")) or {}
                    b_id = b_data.get("id") or b_file.stem
                    b_rel = f"work-orders/BLOCKED/{b_file.name}"
                    b_status = str(b_data.get("status") or "BLOCKED").upper()
                    if b_id in existing_orders:
                        b_entry = existing_orders[b_id]
                        if b_entry.get("status") != b_status:
                            b_entry["status"] = b_status
                            updated_index = True
                        if b_entry.get("file") != b_rel:
                            b_entry["file"] = b_rel
                            updated_index = True
                        if not b_entry.get("priority"):
                            b_entry["priority"] = b_data.get("priority", "P1")
                            updated_index = True
                        if b_entry.get("dependencies") is None:
                            b_entry["dependencies"] = list(b_data.get("dependencies") or [])
                            updated_index = True
                    else:
                        b_entry = {
                            "id": b_id,
                            "type": b_data.get("type", "FEATURE"),
                            "title": b_data.get("title", f"Work order {b_id}"),
                            "status": b_status,
                            "priority": b_data.get("priority", "P0"),
                            "assigned_agents": b_data.get("assigned_agents", ["codex"]),
                            "dependencies": b_data.get("dependencies", []),
                            "created": str(b_data.get("created") or now_iso),
                            "updated": str(b_data.get("updated") or now_iso),
                            "file": b_rel,
                        }
                        if b_data.get("deliverable"):
                            b_entry["deliverable"] = b_data["deliverable"]
                        orders.append(b_entry)
                        existing_orders[b_id] = b_entry
                        updated_index = True

            tot_active = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
            )
            tot_completed = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
            )
            tot_blocked = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
            )
            if index_data.get("total_active") != tot_active:
                index_data["total_active"] = tot_active
                updated_index = True
            if index_data.get("total_completed") != tot_completed:
                index_data["total_completed"] = tot_completed
                updated_index = True
            if index_data.get("total_blocked") != tot_blocked:
                index_data["total_blocked"] = tot_blocked
                updated_index = True

            for o in orders:
                if isinstance(o, dict):
                    if not o.get("priority"):
                        o["priority"] = "P1"
                        updated_index = True
                    if o.get("dependencies") is None:
                        o["dependencies"] = []
                        updated_index = True
                    if not o.get("file") and o.get("path"):
                        clean_path = str(o["path"]).replace("\\", "/")
                        if clean_path.startswith(".sync/"):
                            clean_path = clean_path.replace(".sync/", "", 1)
                        o["file"] = clean_path
                        updated_index = True

            numeric_ids = []
            for o in orders:
                if isinstance(o, dict):
                    oid = o.get("id", "")
                    if isinstance(oid, str) and oid.startswith("WO-"):
                        try:
                            numeric_ids.append(int(oid.split("-")[1]))
                        except (ValueError, IndexError):
                            pass
            if numeric_ids:
                max_id = max(numeric_ids)
                if index_data.get("next_id", 0) <= max_id:
                    index_data["next_id"] = max_id + 1
                    updated_index = True

            if updated_index:
                index_data["last_updated"] = now_iso
                temp_idx = index_file.with_name(f"{index_file.name}.tmp.{int(time.time() * 1000)}")
                temp_idx.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")
                temp_idx.replace(index_file)

            # Also ensure work_orders totals and assigned_work_orders in TREE.yaml are updated
            tree_file = sync_dir / "runtime" / "TREE.yaml"
            from cli.init import ensure_canonical_tree
            tree_data = ensure_canonical_tree(base_dir)
            updated_tree = False
            if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
                for k in ("total_active", "total_completed", "total_blocked"):
                    if tree_data["work_orders"].get(k) != index_data.get(k, 0):
                        tree_data["work_orders"][k] = index_data.get(k, 0)
                        updated_tree = True
            if "agents" in tree_data and isinstance(tree_data["agents"], dict):
                completed_set = {
                    o.get("id") for o in orders
                    if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
                }
                for ag_name, ag_entry in tree_data["agents"].items():
                    if isinstance(ag_entry, dict) and "assigned_work_orders" in ag_entry:
                        curr = ag_entry["assigned_work_orders"]
                        if isinstance(curr, list):
                            filtered = [w for w in curr if w not in completed_set]
                            if filtered != curr:
                                ag_entry["assigned_work_orders"] = filtered
                                updated_tree = True
                for ag_name, wo_ids in assigned_per_agent.items():
                    if ag_name in tree_data["agents"]:
                        ag_entry = tree_data["agents"][ag_name]
                        if isinstance(ag_entry, dict):
                            curr_assigned = ag_entry.setdefault("assigned_work_orders", [])
                            for w in wo_ids:
                                if w not in curr_assigned:
                                    curr_assigned.append(w)
                                    updated_tree = True
            if updated_tree:
                from validators.kernel.io import durable_write_yaml
                durable_write_yaml(tree_file, tree_data)
        except Exception:
            pass
