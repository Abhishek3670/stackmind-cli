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


from validators.clock import now as _now


def _is_test_path(path: str) -> bool:
    """True when a path denotes a test artifact (tests/ dir or test_ naming)."""
    norm = str(path).replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm:
        return False
    parts = norm.lower().split("/")
    if parts[0] in ("tests", "test"):
        return True
    stem = Path(norm).stem.lower()
    return stem.startswith("test_") or stem.endswith("_test")


def _slugify(title: str, max_len: int = 48) -> str:
    words = re.sub(r"[^a-zA-Z0-9]+", "_", str(title or "")).strip("_").lower()
    words = re.sub(r"_+", "_", words)
    if len(words) > max_len:
        words = words[:max_len].rstrip("_")
    return words or "qa_suite"


def extract_deliverable_spec(milestone_title: str, tasks: list[str], role: str) -> dict[str, str]:
    """Extract deliverable type, path, and description from milestone and tasks."""
    combined_text = f"{milestone_title}\n" + "\n".join(tasks)
    
    # 1. Search for explicitly mentioned file paths in tasks
    # Patterns: requirements.txt, src/backend.py, src/frontend.html, src/landing.html, etc.
    file_pattern = r"(?:`|\"|'|\s|^)([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,6})(?:`|\"|'|\s|$)"
    matches = re.findall(file_pattern, combined_text)
    candidate_paths: list[str] = []
    for m in matches:
        clean = m.strip().strip("`\"'.,()")
        if clean.endswith((".txt", ".py", ".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".md", ".json", ".yaml", ".yml")):
            if not clean.startswith(".sync") and not clean.startswith(".git") and clean != "PLAN.md":
                candidate_paths.append(clean)

    role_norm = role.lower().strip()
    
    # Role-based extraction with candidate path matching
    if role_norm in ("gemini", "frontend"):
        target_path = next(
            (p for p in candidate_paths if any(p.endswith(ext) for ext in (".html", ".jsx", ".tsx", ".js", ".ts", ".css"))),
            None,
        )
        if not target_path:
            target_path = next(
                (p for p in candidate_paths if not p.endswith((".py", ".txt", ".md"))),
                None,
            )
        if not target_path:
            if "landing" in combined_text.lower():
                target_path = "src/landing.html"
            else:
                target_path = "src/frontend.html"

        return {
            "type": "code",
            "path": target_path,
            "description": f"Frontend interface and UI components for {milestone_title}",
        }

    if role_norm in ("gemma", "qa"):
        # QA work orders deliver an executable test suite the QA worker authors
        # and runs end-to-end — never a bare sign-off document (D024 requires a
        # companion test file for every code deliverable at GitOps time).
        test_path = next((p for p in candidate_paths if _is_test_path(p)), None)
        if not test_path:
            test_path = f"tests/test_{_slugify(milestone_title)}.py"
        return {
            "type": "code",
            "path": test_path,
            "description": (
                f"Executable end-to-end test suite and QA sign-off for {milestone_title}"
            ),
        }

    if role_norm in ("local-llm", "gitops"):
        target_path = next(
            (p for p in candidate_paths if p in ("VERSION.md", "CHANGELOG.md", "package.json")),
            "VERSION.md",
        )
        return {
            "type": "doc",
            "path": target_path,
            "description": f"Release metadata and version bump for {milestone_title}",
        }

    # Default: Codex / Backend
    # Only treat as requirements.txt if explicitly mentioned and no code files are targeted
    has_req = any(p == "requirements.txt" or p.endswith("requirements.txt") for p in candidate_paths) or any(
        kw in combined_text.lower() for kw in ("requirements.txt", "dependency manifest", "dependencies manifest", "setup dependencies")
    )
    code_path = next((p for p in candidate_paths if p.endswith(".py") and not "test" in p), None)
    if not code_path:
        code_path = next((p for p in candidate_paths if p.endswith(".py")), None)
    if has_req and not code_path:
        return {
            "type": "config",
            "path": "requirements.txt",
            "description": "Project dependency manifest declaring required packages",
        }

    target_path = code_path or "src/backend.py"
    return {
        "type": "code",
        "path": target_path,
        "description": f"Backend API and logic implementation for {milestone_title}",
    }


def determine_assigned_agent(title: str, tasks: list[str]) -> tuple[str, str]:
    """Determine (agent_id, role) from milestone title and tasks.

    The Architect's explicit designation in PLAN.md takes top priority.
    """
    combined = (f"{title} " + " ".join(tasks)).lower()
    title_lower = title.lower()

    # 1. Top priority: Explicit agent designation by the Architect in title or tasks
    # Patterns: (Agent: codex), [Agent: codex], Agent: codex, Assigned: codex, Owner: codex,
    #           (codex), [codex], @codex
    target_header = f"{title} " + (tasks[0] if tasks else "")

    agent_match = (
        re.search(r"(?:agent|assigned|owner)\s*[:=]\s*([a-zA-Z0-9_\-]+)", target_header, re.IGNORECASE)
        or re.search(r"[\(\[]\s*(?:agent:\s*)?(codex|gemini|gemma|local-llm|claude)\s*[\)\]]", target_header, re.IGNORECASE)
        or re.search(r"@([a-zA-Z0-9_\-]+)", target_header)
    )
    if agent_match:
        explicit_agent = agent_match.group(1).lower().strip()
        role_map = {
            "codex": "backend",
            "gemini": "frontend",
            "gemma": "qa",
            "local-llm": "gitops",
            "claude": "architecture",
        }
        if explicit_agent in role_map:
            return explicit_agent, role_map[explicit_agent]

    # 2. Explicit role designation by the Architect
    # Patterns: (Role: backend), [Role: frontend], Role: qa, Role: gitops
    role_match = re.search(r"\brole\s*[:=]\s*([a-zA-Z0-9_\-]+)", target_header, re.IGNORECASE)
    if role_match:
        explicit_role = role_match.group(1).lower().strip()
        agent_role_map = {
            "backend": ("codex", "backend"),
            "frontend": ("gemini", "frontend"),
            "qa": ("gemma", "qa"),
            "gitops": ("local-llm", "gitops"),
            "release": ("local-llm", "gitops"),
            "architecture": ("claude", "architecture"),
        }
        if explicit_role in agent_role_map:
            return agent_role_map[explicit_role]

    # 3. Fallback: Semantic heuristics based on title and task keywords
    # Frontend checks: explicit UI files or keywords
    if re.search(r"\b[a-zA-Z0-9_\-\.\/]+\.(?:html|css|jsx|tsx)\b", combined) or re.search(
        r"\b(?:ui|frontend|views?|components?|screens?|css|html|tailwind|react|client|landing page|login page)\b", combined
    ):
        return "gemini", "frontend"

    # GitOps checks
    if re.search(r"\b(?:release|git|deploy|packaging|version|gitops|changelog)\b", combined):
        return "local-llm", "gitops"

    # Implementation / backend logic belongs to Codex (even if tasks include unit tests)
    if re.search(r"\b(?:implement|implementation|backend|feature|logic|module|service|core|tokenbucket|token_bucket|api)\b", title_lower):
        return "codex", "backend"

    # QA checks: pure verification, test suite execution, audit, or review
    if re.search(r"\b(?:qa|verification|audit|validation|review)\b", title_lower) or re.search(
        r"\b(?:qa|tests?|testing|verification|audit|validation|review)\b", combined
    ):
        return "gemma", "qa"

    # Scaffolding / dependencies specifically for backend
    if any(kw in combined for kw in ("requirements.txt", "dependency manifest", "dependencies manifest", "setup dependencies")):
        return "codex", "backend"

    return "codex", "backend"


def build_child_work_order(
    wo_id: str,
    title: str,
    description: str,
    assigned_agents: list[str],
    dependencies: list[str],
    priority: str,
    deliverable: dict[str, str],
    implementation_estimate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a schema-conforming Work Order record."""
    now = _now()
    deliv_spec: dict[str, Any] = {
        "type": deliverable.get("type", "code"),
        "description": deliverable.get("description", f"Deliverables for {title}"),
    }
    if deliverable.get("path"):
        deliv_spec["path"] = deliverable["path"]

    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": title.strip(),
        "status": "ACTIVE",
        "priority": priority,
        "assigned_agents": list(assigned_agents),
        "dependencies": list(dependencies),
        "deliverable": deliv_spec,
        "description": description.strip() or title.strip(),
        "created": now,
        "updated": now,
    }
    if implementation_estimate:
        record["implementation_estimate"] = dict(implementation_estimate)
    return record


def derive_implementation_estimate(
    title: str,
    description: str,
    deliverable_path: str | None,
    candidate_paths: list[str] | None = None,
    deliverable_type: str | None = None,
) -> dict[str, Any] | None:
    """Derive an explicit implementation estimate from the task's expected file set.

    The estimate drives the contract file budget so a scaffold task is never
    silently blocked by an arbitrary fixed limit, and so a task whose expected
    footprint grows beyond a small bounded set is flagged for splitting instead
    of receiving an unbounded budget.

    Every code deliverable also plans its D024 companion test
    (``tests/test_<stem>.py``) so test authoring is part of the work order's
    declared footprint instead of being discovered missing at GitOps time.
    """
    expected: list[str] = []
    if deliverable_path:
        expected.append(str(deliverable_path).replace("\\", "/").strip().lstrip("/"))
    for cp in candidate_paths or []:
        clean = str(cp).replace("\\", "/").strip().lstrip("/")
        if clean and clean not in expected:
            expected.append(clean)
    if not expected:
        return None

    deliv_norm = (deliverable_path or "").replace("\\", "/").strip().lstrip("/")
    code_exts = (".py", ".ts", ".js", ".go", ".rs", ".dart")
    is_code_deliverable = (
        str(deliverable_type or "").lower() == "code"
        or (bool(deliv_norm) and deliv_norm.endswith(code_exts))
    )
    if (
        is_code_deliverable
        and deliv_norm
        and not _is_test_path(deliv_norm)
        and not any(_is_test_path(f) for f in expected)
    ):
        stem = Path(deliv_norm).stem
        expected.append(f"tests/test_{stem}.py")

    return {
        "expected_files": expected,
        "max_files_touched": len(expected),
        "rationale": f"Plan-derived estimate of the files this task creates or modifies: {', '.join(expected)}",
    }


def build_child_contract(
    wo_id: str,
    agent_id: str,
    role: str,
    deliverable_path: str | None = None,
    candidate_paths: list[str] | None = None,
    max_files_touched: int | None = None,
) -> dict[str, Any]:
    """Construct a schema-conforming Contract record bound strictly to the assigned scope.

    ``max_files_touched`` should come from the work order's plan-derived
    implementation estimate.  The conservative default is retained only for
    explicitly designated bootstrap/internal work orders — it must never be
    used to paper over a failed Architect authoring turn.
    """
    role_norm = role.lower().strip()
    allow_rules: list[dict[str, Any]] = [
        {"module": "PLAN.md"},
        {"module": f".sync/inbox/{agent_id}/**"},
    ]

    if deliverable_path:
        allow_rules.append({"module": deliverable_path})

    if candidate_paths:
        for cp in candidate_paths:
            if cp and isinstance(cp, str) and not cp.startswith(".git") and not cp.startswith(".sync") and cp != ".env":
                allow_rules.append({"module": cp})

    # Common repository root documentation and config files allowed across roles:
    allow_rules.extend([
        {"module": "*.md"},
        {"module": "*.txt"},
        {"module": "*.toml"},
        {"module": "*.json"},
        {"module": "*.yaml"},
        {"module": "*.yml"},
        {"module": "README*"},
        {"module": "CHANGELOG*"},
        {"module": "VERSION*"},
        {"module": "LICENSE*"},
        {"module": ".gitignore"},
        {"module": "*ignore"},
        {"module": "Makefile*"},
        # Governance bookkeeping channel: machine-readable recovery decisions
        {"module": ".sync/decisions/**"},
    ])

    if role_norm in ("backend", "codex"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "api/**"},
            {"module": "backend/**"},
            {"module": "db/**"},
            {"module": "models/**"},
            {"module": "services/**"},
            {"module": "routes/**"},
            {"module": "controllers/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "requirements.txt"},
            {"module": "requirements*.txt"},
            {"module": "pyproject.toml"},
            {"module": "setup.py"},
            {"module": "setup.cfg"},
            {"module": "Makefile"},
            {"module": "*.py"},
            {"module": "*.sql"},
            {"module": "*.json"},
            {"module": "*.yaml"},
            {"module": "*.yml"},
        ])
    elif role_norm in ("frontend", "gemini"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "public/**"},
            {"module": "frontend/**"},
            {"module": "static/**"},
            {"module": "templates/**"},
            {"module": "views/**"},
            {"module": "components/**"},
            {"module": "index.html"},
            {"module": "package.json"},
            {"module": "*.html"},
            {"module": "*.js"},
            {"module": "*.ts"},
            {"module": "*.tsx"},
            {"module": "*.jsx"},
            {"module": "*.css"},
        ])
    elif role_norm in ("qa", "gemma"):
        allow_rules.extend([
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "*.py"},
            {"module": "pytest.ini"},
        ])
    elif role_norm in ("gitops", "local-llm"):
        allow_rules.extend([
            {"module": "VERSION.md"},
            {"module": "CHANGELOG.md"},
            {"module": "Makefile"},
            {"module": "pyproject.toml"},
            {"module": "package.json"},
            {"module": "setup.py"},
            {"module": "setup.cfg"},
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "*.py"},
        ])
    else:
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "*.py"},
        ])

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
    # Protect executive inbox from unauthorized worker access
    if agent_id != "CEO":
        deny_rules.append({"module": ".sync/inbox/CEO/**"})
    # Preserve protocol message routing channels (AGENTS.md):
    # - Workers need to drop review requests to .sync/inbox/gemma/
    # - Gemma needs to drop verdicts to .sync/inbox/claude/ and .sync/inbox/codex/
    # - Claude needs to dispatch to worker inboxes
    for other_agent in ("claude", "codex", "gemini", "gemma", "local-llm"):
        if other_agent == agent_id:
            continue
        # Exclude permitted protocol routing channels
        if role_norm in ("qa", "gemma") and other_agent in ("claude", "codex", "gemini"):
            continue
        if role_norm in ("backend", "frontend", "codex", "gemini") and other_agent in ("gemma", "claude"):
            continue
        if role_norm in ("architecture", "claude"):
            continue
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
            "max_files_touched": max_files_touched if max_files_touched and max_files_touched > 0 else 10,
            "max_tokens": 0,
        },
    }


def get_next_work_order_int(workspace: Path | str) -> int:
    """Find the next monotonically increasing integer ID for work orders.

    Scans:
    1. .sync/work-orders/ACTIVE/
    2. .sync/work-orders/COMPLETED/
    3. .sync/work-orders/INDEX.yaml

    Returns max(existing_ids) + 1, starting at 1 if no existing worker WOs.
    """
    ws = Path(workspace).resolve()
    max_id = 0
    pattern = re.compile(r"^WO-([0-9]{3,})$")

    # 1. Check INDEX.yaml
    index_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
    if index_file.is_file():
        try:
            data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                next_id = data.get("next_id")
                if isinstance(next_id, int) and next_id > max_id:
                    max_id = next_id - 1
                for o in data.get("orders", []):
                    if isinstance(o, dict) and o.get("id"):
                        m = pattern.match(str(o["id"]))
                        if m:
                            val = int(m.group(1))
                            if val > max_id:
                                max_id = val
        except Exception:
            pass

    # 2. Check ACTIVE, COMPLETED, and BLOCKED directories
    for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
        d = ws / ".sync" / "work-orders" / sub
        if d.is_dir():
            for f in d.glob("*.yaml"):
                m = pattern.match(f.stem)
                if m:
                    val = int(m.group(1))
                    if val > max_id:
                        max_id = val

    return max_id + 1


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
    start_idx = get_next_work_order_int(ws)

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
                for offset, m in enumerate(parsed.milestones):
                    idx = start_idx + offset
                    agent, role = determine_assigned_agent(m.title, m.tasks)
                    deliv = extract_deliverable_spec(m.title, m.tasks, role)
                    wo_id = f"WO-{idx:03d}"
                    deps = [f"WO-{idx-1:03d}"] if offset > 0 else []
                    wo = build_child_work_order(
                        wo_id=wo_id,
                        title=m.title,
                        description="\n".join(m.tasks) if m.tasks else m.title,
                        assigned_agents=[agent],
                        dependencies=deps,
                        priority="P0" if offset == 0 else "P1",
                        deliverable=deliv,
                    )
                    proposed_wos.append(wo)
            except Exception:
                pass
    elif isinstance(plan, str):
        try:
            parsed = parse_plan(plan)
            for offset, m in enumerate(parsed.milestones):
                idx = start_idx + offset
                agent, role = determine_assigned_agent(m.title, m.tasks)
                deliv = extract_deliverable_spec(m.title, m.tasks, role)
                wo_id = f"WO-{idx:03d}"
                deps = [f"WO-{idx-1:03d}"] if offset > 0 else []
                wo = build_child_work_order(
                    wo_id=wo_id,
                    title=m.title,
                    description="\n".join(m.tasks) if m.tasks else m.title,
                    assigned_agents=[agent],
                    dependencies=deps,
                    priority="P0" if offset == 0 else "P1",
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
                for offset, m in enumerate(parsed.milestones):
                    idx = start_idx + offset
                    agent, role = determine_assigned_agent(m.title, m.tasks)
                    deliv = extract_deliverable_spec(m.title, m.tasks, role)
                    wo_id = f"WO-{idx:03d}"
                    deps = [f"WO-{idx-1:03d}"] if offset > 0 else []
                    wo = build_child_work_order(
                        wo_id=wo_id,
                        title=m.title,
                        description="\n".join(m.tasks) if m.tasks else m.title,
                        assigned_agents=[agent],
                        dependencies=deps,
                        priority="P0" if offset == 0 else "P1",
                        deliverable=deliv,
                    )
                    proposed_wos.append(wo)
            except Exception:
                pass

    if not proposed_wos:
        raise ValueError("Cannot synthesize child work orders: no milestones or tasks could be parsed from plan")

    # Anti-collision check: if any proposed WO conflicts with existing on-disk work order, remap IDs sequentially
    existing_on_disk = set()
    for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
        d = ws / ".sync" / "work-orders" / sub
        if d.is_dir():
            for f in d.glob("*.yaml"):
                existing_on_disk.add(f.stem)

    if any(w.get("id") in existing_on_disk for w in proposed_wos):
        id_map: dict[str, str] = {}
        renumbered_wos: list[dict[str, Any]] = []
        for offset, w in enumerate(proposed_wos):
            old_id = str(w.get("id", f"WO-{offset+1:03d}"))
            new_id = f"WO-{start_idx + offset:03d}"
            id_map[old_id] = new_id
            w_copy = dict(w)
            w_copy["id"] = new_id
            new_deps = [id_map.get(d, d) for d in w.get("dependencies", [])]
            w_copy["dependencies"] = new_deps
            renumbered_wos.append(w_copy)
        proposed_wos = renumbered_wos

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

        # Refine agent assignment if defaulted to codex but clearly targets frontend or QA
        detected_agent, detected_role = determine_assigned_agent(title, [desc])
        if primary_agent == "codex" and detected_agent in ("gemini", "gemma", "local-llm"):
            primary_agent = detected_agent

        role = "backend"
        if primary_agent == "gemini":
            role = "frontend"
        elif primary_agent == "gemma":
            role = "qa"
        elif primary_agent in ("local-llm", "gitops"):
            role = "gitops"

        # Ensure deliverable has explicit path matching role
        if not deliv.get("path") or (primary_agent == "gemini" and str(deliv.get("path")).endswith(".txt")):
            deliv = extract_deliverable_spec(title, [desc], role)

        # Extract candidate paths from title and description
        combined_task_text = f"{title}\n{desc}"
        cand_matches = re.findall(
            r"(?:`|\"|'|\s|^)([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,6})(?:`|\"|'|\s|$)",
            combined_task_text,
        )
        task_candidates: list[str] = []
        for cm in cand_matches:
            c_clean = cm.strip().strip("`\"'.,()")
            if any(c_clean.endswith(ext) for ext in (".txt", ".py", ".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".md", ".json", ".yaml", ".yml", ".sql")):
                if not c_clean.startswith(".sync") and not c_clean.startswith(".git") and c_clean not in ("PLAN.md", ".env"):
                    task_candidates.append(c_clean)

        # Plan-derived file budget: the contract covers exactly the files the
        # task is expected to create/modify (including its D024 companion test)
        # instead of a fixed limit.
        implementation_estimate = derive_implementation_estimate(
            title, desc, deliv.get("path"), task_candidates,
            deliverable_type=deliv.get("type"),
        )

        contract_rec = build_child_contract(
            wo_id=wo_id,
            agent_id=primary_agent,
            role=role,
            deliverable_path=deliv.get("path"),
            candidate_paths=task_candidates,
            max_files_touched=(
                implementation_estimate["max_files_touched"] if implementation_estimate else None
            ),
        )

        wo_rec = build_child_work_order(
            wo_id=wo_id,
            title=title,
            description=desc,
            assigned_agents=[primary_agent],
            dependencies=deps,
            priority=prio,
            deliverable=deliv,
            implementation_estimate=implementation_estimate,
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

    max_id_num = 0
    pattern = re.compile(r"^WO-([0-9]{3,})$")
    for item in index_data.get("orders", []):
        if isinstance(item, dict) and item.get("id"):
            m = pattern.match(str(item["id"]))
            if m:
                max_id_num = max(max_id_num, int(m.group(1)))
    index_data["next_id"] = max_id_num + 1

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
