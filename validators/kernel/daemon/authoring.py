"""Autonomous synthesis and validation of child Work Orders and Contracts (CONTRACT-01 & LEARN-01).

Derives governed, schema-validated Work Orders and Contracts from approved PLAN.md
milestones, ensuring fail-closed compliance with AuthoringGate and schemas across
both tool-using cloud models and conversational local models (Ollama, vLLM).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.plan import parse_plan, PlanStructure


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_deliverable_spec(milestone_title: str, tasks: list[str], role: str) -> dict[str, str]:
    """Extract deliverable type, path, and description from milestone and tasks."""
    combined_text = f"{milestone_title}\n" + "\n".join(tasks)
    
    # 1. Search for explicitly mentioned file paths in tasks
    # Patterns: requirements.txt, src/backend.py, src/frontend.html, etc.
    file_pattern = r"(?:`|\"|'|\s|^)([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,6})(?:`|\"|'|\s|$)"
    matches = re.findall(file_pattern, combined_text)
    candidate_paths: list[str] = []
    for m in matches:
        clean = m.strip().strip("`\"'.,()")
        if clean.endswith((".txt", ".py", ".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".md", ".json", ".yaml", ".yml")):
            if not clean.startswith(".sync") and not clean.startswith(".git") and clean != "PLAN.md":
                candidate_paths.append(clean)

    role_norm = role.lower().strip()
    
    # Scaffolding / requirements check
    if any(kw in combined_text.lower() for kw in ("requirements.txt", "scaffold", "dependency manifest", "dependencies manifest", "setup dependencies")):
        return {
            "type": "config",
            "path": "requirements.txt",
            "description": "Project dependency manifest declaring required packages",
        }

    # Role-based extraction with candidate path fallback
    if role_norm in ("gemini", "frontend"):
        target_path = next(
            (p for p in candidate_paths if any(p.endswith(ext) for ext in (".html", ".jsx", ".tsx", ".js", ".ts", ".css"))),
            "src/frontend.html",
        )
        return {
            "type": "code",
            "path": target_path,
            "description": f"Frontend interface and UI components for {milestone_title}",
        }

    if role_norm in ("gemma", "qa"):
        target_path = next(
            (p for p in candidate_paths if "test" in p),
            "tests/test_login.py",
        )
        return {
            "type": "code",
            "path": target_path,
            "description": f"Test suite and validation for {milestone_title}",
        }

    if role_norm in ("local-llm", "gitops"):
        target_path = next(
            (p for p in candidate_paths if p in ("VERSION.md", "CHANGELOG.md")),
            "VERSION.md",
        )
        return {
            "type": "doc",
            "path": target_path,
            "description": f"Release metadata and version bump for {milestone_title}",
        }

    # Default: Codex / Backend
    target_path = next(
        (p for p in candidate_paths if p.endswith(".py") and not p.startswith("test")),
        "src/backend.py",
    )
    return {
        "type": "code",
        "path": target_path,
        "description": f"Backend API and logic implementation for {milestone_title}",
    }


def determine_assigned_agent(title: str, tasks: list[str]) -> tuple[str, str]:
    """Determine (agent_id, role) from milestone title and tasks."""
    combined = (f"{title} " + " ".join(tasks)).lower()

    # Scaffolding / dependencies are always Backend (Codex)
    if any(kw in combined for kw in ("requirements.txt", "scaffold", "dependency manifest", "dependencies manifest", "setup dependencies")):
        return "codex", "backend"

    if re.search(r"\b(?:ui|frontend|views?|components?|screens?|css|html|react|client|login page)\b", combined):
        return "gemini", "frontend"
    if re.search(r"\b(?:qa|tests?|testing|verification|audit|validation|review)\b", combined):
        return "gemma", "qa"
    if re.search(r"\b(?:release|git|deploy|packaging|version|gitops|changelog)\b", combined):
        return "local-llm", "gitops"
    return "codex", "backend"


def build_child_work_order(
    wo_id: str,
    title: str,
    description: str,
    assigned_agents: list[str],
    dependencies: list[str],
    priority: str,
    deliverable: dict[str, str],
) -> dict[str, Any]:
    """Construct a schema-conforming Work Order record."""
    now = _now()
    return {
        "id": wo_id,
        "type": "FEATURE",
        "title": title.strip(),
        "status": "ACTIVE",
        "priority": priority,
        "assigned_agents": list(assigned_agents),
        "dependencies": list(dependencies),
        "deliverable": {
            "type": deliverable.get("type", "code"),
            "path": deliverable.get("path"),
            "description": deliverable.get("description", f"Deliverables for {title}"),
        },
        "description": description.strip() or title.strip(),
        "created": now,
        "updated": now,
    }


def build_child_contract(
    wo_id: str,
    agent_id: str,
    role: str,
    deliverable_path: str | None = None,
) -> dict[str, Any]:
    """Construct a schema-conforming Contract record bound strictly to the assigned scope."""
    role_norm = role.lower().strip()
    allow_rules: list[dict[str, Any]] = [
        {"module": "PLAN.md"},
        {"module": f".sync/inbox/{agent_id}/**"},
    ]

    if deliverable_path:
        allow_rules.append({"module": deliverable_path})

    if role_norm in ("backend", "codex"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "tests/**"},
            {"module": "requirements.txt"},
            {"module": "pyproject.toml"},
        ])
    elif role_norm in ("frontend", "gemini"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "public/**"},
            {"module": "frontend/**"},
            {"module": "index.html"},
        ])
    elif role_norm in ("qa", "gemma"):
        allow_rules.extend([
            {"module": "tests/**"},
            {"module": "src/**"},
        ])
    elif role_norm in ("gitops", "local-llm"):
        allow_rules.extend([
            {"module": "VERSION.md"},
            {"module": "CHANGELOG.md"},
            {"module": "pyproject.toml"},
            {"module": "package.json"},
        ])
    else:
        allow_rules.append({"module": "src/**"})

    # Deduplicate allow rules preserving structure
    seen = set()
    unique_allow = []
    for r in allow_rules:
        mod = r.get("module")
        if mod and mod not in seen:
            seen.add(mod)
            unique_allow.append(r)

    deny_rules = [
        {"module": ".git/**"},
        {"module": ".env"},
        {"module": ".env.*"},
        {"module": ".sync/runtime/**"},
        {"module": ".sync/knowledge/**"},
        {"module": ".sync/snapshots/**"},
        {"module": ".sync/outbox/**"},
        {"module": ".sync/agents/**"},
        {"module": "__pycache__/**"},
        {"module": ".venv/**"},
        {"module": "node_modules/**"},
    ]
    # Deny other agents' private inboxes
    for other_agent in ("claude", "codex", "gemini", "gemma", "local-llm", "CEO"):
        if other_agent != agent_id:
            deny_rules.append({"module": f".sync/inbox/{other_agent}/**"})

    return {
        "schema_version": 1,
        "agent_id": agent_id,
        "work_order": wo_id,
        "identity": {
            "role": role_norm,
            "reports_to": "claude",
        },
        "scope": {
            "allow": unique_allow,
            "deny": deny_rules,
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": 10,
            "max_tokens": 40000,
        },
    }


def synthesize_child_work_orders(
    workspace: Path | str,
    plan: dict[str, Any] | str,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Synthesize validated child Work Orders and Contracts on disk from an approved plan.

    All artifacts are strictly verified fail-closed against AuthoringGate and schemas.
    Updates .sync/work-orders/ACTIVE/, .sync/contracts/, INDEX.yaml, and TREE.yaml.
    """
    ws = Path(workspace).resolve()
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    gate = AuthoringGate(project_root=ws)

    # 1. Extract plan structure and proposed work orders
    proposed_wos: list[dict[str, Any]] = []

    if isinstance(plan, dict):
        # Check if pre-parsed work orders exist in plan metadata
        metadata = plan.get("metadata", {})
        if metadata.get("work_orders"):
            proposed_wos = list(metadata["work_orders"])
        elif plan.get("created_work_orders"):
            proposed_wos = list(plan["created_work_orders"])
        elif plan.get("content"):
            try:
                parsed = parse_plan(plan["content"])
                for idx, m in enumerate(parsed.milestones, start=1):
                    agent, role = determine_assigned_agent(m.title, m.tasks)
                    deliv = extract_deliverable_spec(m.title, m.tasks, role)
                    wo_id = f"WO-{idx:03d}"
                    deps = [f"WO-{idx-1:03d}"] if idx > 1 else []
                    wo = build_child_work_order(
                        wo_id=wo_id,
                        title=m.title,
                        description="\n".join(m.tasks) if m.tasks else m.title,
                        assigned_agents=[agent],
                        dependencies=deps,
                        priority="P0" if idx == 1 else "P1",
                        deliverable=deliv,
                    )
                    proposed_wos.append(wo)
            except Exception:
                pass
    elif isinstance(plan, str):
        try:
            parsed = parse_plan(plan)
            for idx, m in enumerate(parsed.milestones, start=1):
                agent, role = determine_assigned_agent(m.title, m.tasks)
                deliv = extract_deliverable_spec(m.title, m.tasks, role)
                wo_id = f"WO-{idx:03d}"
                deps = [f"WO-{idx-1:03d}"] if idx > 1 else []
                wo = build_child_work_order(
                    wo_id=wo_id,
                    title=m.title,
                    description="\n".join(m.tasks) if m.tasks else m.title,
                    assigned_agents=[agent],
                    dependencies=deps,
                    priority="P0" if idx == 1 else "P1",
                    deliverable=deliv,
                )
                proposed_wos.append(wo)
        except Exception:
            pass

    # Fallback to reading PLAN.md directly if still empty
    if not proposed_wos:
        plan_file = ws / "PLAN.md"
        if plan_file.is_file():
            try:
                parsed = parse_plan(plan_file.read_text(encoding="utf-8"))
                for idx, m in enumerate(parsed.milestones, start=1):
                    agent, role = determine_assigned_agent(m.title, m.tasks)
                    deliv = extract_deliverable_spec(m.title, m.tasks, role)
                    wo_id = f"WO-{idx:03d}"
                    deps = [f"WO-{idx-1:03d}"] if idx > 1 else []
                    wo = build_child_work_order(
                        wo_id=wo_id,
                        title=m.title,
                        description="\n".join(m.tasks) if m.tasks else m.title,
                        assigned_agents=[agent],
                        dependencies=deps,
                        priority="P0" if idx == 1 else "P1",
                        deliverable=deliv,
                    )
                    proposed_wos.append(wo)
            except Exception:
                pass

    if not proposed_wos:
        raise ValueError("Cannot synthesize child work orders: no milestones or tasks could be parsed from plan")

    created_records: list[dict[str, Any]] = []

    # 2. Validate and write each Work Order and Contract
    for wo in proposed_wos:
        wo_id = str(wo.get("id"))
        if not re.match(r"^WO-[0-9]{3}$", wo_id):
            continue

        assigned = wo.get("assigned_agents", ["codex"])
        primary_agent = assigned[0] if assigned else "codex"
        title = wo.get("title", f"Work order {wo_id}")
        desc = wo.get("description", title)
        deps = wo.get("dependencies", [])
        prio = wo.get("priority", "P1")
        deliv = wo.get("deliverable") or {}

        # Ensure deliverable has explicit path
        if not deliv.get("path"):
            deliv = extract_deliverable_spec(title, [desc], primary_agent)

        wo_rec = build_child_work_order(
            wo_id=wo_id,
            title=title,
            description=desc,
            assigned_agents=[primary_agent],
            dependencies=deps,
            priority=prio,
            deliverable=deliv,
        )

        role = "backend"
        if primary_agent == "gemini":
            role = "frontend"
        elif primary_agent == "gemma":
            role = "qa"
        elif primary_agent in ("local-llm", "gitops"):
            role = "gitops"

        contract_rec = build_child_contract(
            wo_id=wo_id,
            agent_id=primary_agent,
            role=role,
            deliverable_path=deliv.get("path"),
        )

        wo_yaml = yaml.safe_dump(wo_rec, sort_keys=False)
        contract_yaml = yaml.safe_dump(contract_rec, sort_keys=False)

        rel_wo_path = f".sync/work-orders/ACTIVE/{wo_id}.yaml"
        rel_contract_path = f".sync/contracts/{wo_id}.yaml"

        # Validate against AuthoringGate (acting on behalf of architecture)
        wo_dec = gate.validate_artifact_content(rel_wo_path, wo_yaml, agent="architecture", project_root=ws)
        if not wo_dec.passed:
            raise ValueError(f"Synthesized work order '{wo_id}' failed authoring gate: {wo_dec.summary}")

        contract_dec = gate.validate_artifact_content(rel_contract_path, contract_yaml, agent="architecture", project_root=ws)
        if not contract_dec.passed:
            raise ValueError(f"Synthesized contract for '{wo_id}' failed authoring gate: {contract_dec.summary}")

        # Write to disk
        (active_dir / f"{wo_id}.yaml").write_text(wo_yaml, encoding="utf-8")
        (contracts_dir / f"{wo_id}.yaml").write_text(contract_yaml, encoding="utf-8")
        created_records.append(wo_rec)

    # 3. Update .sync/work-orders/INDEX.yaml
    index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
    index_data: dict[str, Any] = {"schema_version": 1, "next_id": len(created_records) + 1, "orders": []}
    if index_path.exists():
        try:
            loaded_index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded_index, dict) and "orders" in loaded_index:
                index_data = loaded_index
        except Exception:
            pass

    existing_order_ids = {
        item.get("id") for item in index_data.get("orders", []) if isinstance(item, dict)
    }
    for rec in created_records:
        rec_id = rec["id"]
        if rec_id not in existing_order_ids:
            index_data.setdefault("orders", []).append({
                "id": rec_id,
                "type": rec["type"],
                "title": rec["title"],
                "status": rec["status"],
                "priority": rec["priority"],
                "assigned_agents": rec["assigned_agents"],
                "dependencies": rec["dependencies"],
                "deliverable": rec["deliverable"],
                "created": rec["created"],
                "updated": rec["updated"],
                "file": f"work-orders/ACTIVE/{rec_id}.yaml",
            })
            existing_order_ids.add(rec_id)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    # 4. Update .sync/runtime/TREE.yaml
    tree_path = ws / ".sync" / "runtime" / "TREE.yaml"
    tree_data: dict[str, Any] = {}
    if tree_path.exists():
        try:
            loaded_tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
            if isinstance(loaded_tree, dict):
                tree_data = loaded_tree
        except Exception:
            pass

    tree_data.setdefault("schema_version", 1)
    tree_data.setdefault("tree_version", 1)
    tree_data.setdefault("release", "3.1.0")
    agents_map = tree_data.setdefault("agents", {})
    if not isinstance(agents_map, dict):
        agents_map = {}
        tree_data["agents"] = agents_map

    for rec in created_records:
        rec_id = rec["id"]
        for agent in rec.get("assigned_agents", []):
            agent_info = agents_map.setdefault(agent, {
                "session_count": 0,
                "status": "idle",
                "assigned_work_orders": [],
            })
            assigned_list = agent_info.setdefault("assigned_work_orders", [])
            if rec_id not in assigned_list:
                assigned_list.append(rec_id)

    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")

    return created_records
