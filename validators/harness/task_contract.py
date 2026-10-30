"""Deterministic worker task contract — compiled prompt + in-turn ownership rules.

The worker prompt is COMPILED, not hand-written: the same structured sources
the gates read (work order YAML, contract YAML, peer work orders, harness
output schema) are rendered into one JSON task contract, so every requirement
the harness enforces is displayed to the model exactly once and exactly as
enforced.  The contract also drives the in-turn ownership guard (read-only
peer paths, file budget) so prompt and enforcement cannot drift apart.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Decision output contract — mirrors schemas/harness-output.schema.json (the
# harness validates against that schema with bounded feedback retries).
DECISION_STATUS_ENUM = ("completed", "blocked", "deferred")

def render_decision_contract_text(deliverable: str | None = None) -> str:
    target = f'"{deliverable}"' if deliverable else '"patch"'
    return (
        "FINAL OUTPUT CONTRACT — your last message must be ONE JSON object, no "
        "markdown fences, no commentary:\n"
        '{"status": "completed" | "blocked" | "deferred",\n'
        ' "summary": "<one-line summary>",\n'
        ' "report_markdown": "<what you did>",\n'
        ' "blockers": [],\n'
        ' "modified_files": ["<exact paths you wrote with write_file this turn>"],\n'
        f' "release_target": {target}}}\n'
        "Rules: status 'blocked' REQUIRES a non-empty blockers list.  modified_files "
        "must list ONLY files you actually wrote with write_file this turn, at the "
        "exact paths you used — never a file you did not write, never a renamed or "
        "extra-nested variant.  The harness verifies every claim against the tool "
        "log; do not claim tests passed, do not claim files exist — report only "
        "what your tool calls did."
    )


DECISION_CONTRACT_TEXT = render_decision_contract_text("patch")

_PLANNING_WO_IDS = {"WO-000"}
_GLOB_CHARS = re.compile(r"[*?\[]")


def _norm(path_value: Any) -> str:
    return str(path_value or "").replace("\\", "/").strip().lstrip("/").rstrip("/")


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_CHARS.search(pattern))


def collect_peer_deliverables(workspace: Path | str, own_wo_id: str | None) -> list[tuple[str, str]]:
    """Deliverable paths declared by OTHER active work orders: (wo_id, path)."""
    active_dir = Path(workspace) / ".sync" / "work-orders" / "ACTIVE"
    peers: list[tuple[str, str]] = []
    if not active_dir.is_dir():
        return peers
    for wo_file in sorted(active_dir.glob("*.yaml")):
        if wo_file.stem == own_wo_id or wo_file.stem in _PLANNING_WO_IDS:
            continue
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        deliverable = data.get("deliverable")
        if isinstance(deliverable, dict):
            path_value = _norm(deliverable.get("path"))
            if path_value:
                peers.append((wo_file.stem, path_value))
    return peers


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _safe_clip(text: Any, limit: int) -> str:
    """Clip untrusted text for a prompt: drop control characters first.

    Amendment reasons and changed-field names can originate from model output
    or hand-edited YAML. They are rendered as prompt data, never as
    instructions, so embedded newlines/escapes are stripped before clipping.
    """
    raw = str(text or "")
    cleaned = "".join(
        ch for ch in raw if ch in (" ", "\t") or ord(ch) >= 32 and ch != "\x7f"
    )
    return _clip(cleaned, limit)


def render_work_order_board(
    workspace: Path | str,
    *,
    exclude_wo: str | None = None,
    limit: int = 30,
) -> str:
    """Compact read-only snapshot of sibling work orders for Architect turns.

    Pull-based artefact state only: what each peer task asks for, who owns it,
    and how it has been amended.  Nothing here reflects a live worker turn.
    Returns "" when the board has no entries.
    """
    base = Path(workspace) / ".sync" / "work-orders"
    rows: list[str] = []
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "PENDING"):
        directory = base / bucket
        if not directory.is_dir():
            continue
        for wo_file in sorted(directory.glob("*.yaml")):
            if wo_file.stem == exclude_wo or wo_file.stem in _PLANNING_WO_IDS:
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            agents = ", ".join(str(a) for a in data.get("assigned_agents") or []) or "unassigned"
            deliverable = data.get("deliverable")
            path_value = _norm(deliverable.get("path")) if isinstance(deliverable, dict) else ""
            deps = ", ".join(str(d) for d in data.get("dependencies") or []) or "none"
            revision = data.get("revision")
            header = (
                f"- {wo_file.stem} [{data.get('status') or bucket}] owner={agents}"
                f" deliverable={path_value or 'undeclared'} deps={deps}"
                + (f" revision={revision}" if revision is not None else "")
            )
            lines = [header, f"  title: {_clip(data.get('title'), 120)}"]
            err_msg = data.get("error") or data.get("blocked_reason")
            if err_msg and isinstance(err_msg, str) and err_msg.strip():
                lines.append(f"  blocker: {_clip(err_msg.strip(), 160)}")
            criteria = [
                _clip(c, 120) for c in data.get("acceptance_criteria") or []
                if isinstance(c, str) and c.strip()
            ][:4]
            if criteria:
                lines.append("  acceptance: " + " | ".join(criteria))
            amendments = [
                a for a in data.get("amendments") or []
                if isinstance(a, dict) and str(a.get("reason") or "").strip()
            ]
            if amendments:
                last = amendments[-1]
                lines.append(
                    f"  last amendment (rev {last.get('revision')}): {_clip(last.get('reason'), 160)}"
                )
            rows.append("\n".join(lines))
    if not rows:
        return ""
    shown = rows[:limit]
    omitted = len(rows) - len(shown)
    tail = f"\n... and {omitted} more work order(s) not shown.\n" if omitted else ""
    return (
        "SIBLING WORK ORDER BOARD (read-only snapshot of .sync/work-orders; "
        "the operator's live view is not available to you, and in-flight turns are invisible):\n"
        + "\n".join(shown)
        + tail
        + "Use this to keep your authoring consistent with peers (no overlapping file "
        "ownership), to sharpen a peer's acceptance criteria, or to amend a peer's task "
        "through update_work_order with an explicit amendment reason."
    )


def render_team_status(
    workspace: Path | str,
    *,
    current_wo: str | None = None,
    limit: int = 30,
) -> str:
    """Render comprehensive live team and work order status for Lead Architect context.

    Provides the Lead Architect with complete situational awareness of:
    1. Specialist engineering team status (Architecture, Frontend, Backend, QA, GitOps)
    2. Active blockers, defect findings, and deliverable verification errors
    3. Complete work order board across lifecycle states (COMPLETED, ACTIVE, BLOCKED)
    """
    ws = Path(workspace)
    base = ws / ".sync" / "work-orders"
    if not base.is_dir():
        return ""

    # 1. Parse all work orders across buckets
    wo_map: dict[str, dict[str, Any]] = {}
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "PENDING"):
        b_dir = base / bucket
        if not b_dir.is_dir():
            continue
        for wo_file in sorted(b_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    wo_id = str(data.get("id") or wo_file.stem)
                    data["_bucket"] = bucket
                    data["_file"] = wo_file
                    wo_map[wo_id] = data
            except Exception:
                continue

    if not wo_map:
        return ""

    # 2. Build Agent Team Status
    roles_def = [
        ("Architecture", "claude", "Lead Architect (system design & blocker resolution)"),
        ("Frontend", "gemini", "Frontend Specialist (HTML/CSS/JS, UI, styling)"),
        ("Backend", "codex", "Backend Specialist (APIs, services, logic)"),
        ("QA", "gemma", "QA Specialist (tests, accessibility, verification)"),
        ("GitOps", "local-llm", "GitOps Specialist (versioning, release manifests)"),
    ]

    # 2. Check supervisor run state for orchestration phase and blockers first
    sup_dir = ws / ".sync" / "runtime" / "supervisor"
    supervisor_status_lines: list[str] = []
    active_blockers: list[str] = []
    latest_phase: str | None = None
    if sup_dir.is_dir():
        run_files = sorted(
            sup_dir.glob("run-*.yaml"),
            key=lambda p: p.stat().st_mtime if p.is_file() else 0,
            reverse=True,
        )
        for rf in run_files[:2]:
            try:
                rdata = yaml.safe_load(rf.read_text(encoding="utf-8"))
                if isinstance(rdata, dict):
                    r_id = rdata.get("run_id") or rf.stem
                    r_phase = str(rdata.get("phase") or "UNKNOWN").upper()
                    if latest_phase is None:
                        latest_phase = r_phase
                    r_err = rdata.get("error")
                    r_issues = rdata.get("readiness_issues") or []
                    supervisor_status_lines.append(f"- Run: {r_id} | Phase: {r_phase}")
                    if r_err:
                        err_clean = _clip(str(r_err).strip(), 240)
                        supervisor_status_lines.append(f"  Error: {err_clean}")
                        active_blockers.append(f"Supervisor [{r_id}] ({r_phase}): {err_clean}")
                    for issue in r_issues:
                        if isinstance(issue, dict):
                            i_code = issue.get("code") or "ISSUE"
                            i_msg = _clip(str(issue.get("message") or "").strip(), 160)
                            active_blockers.append(f"Readiness Gate Blocker [{i_code}]: {i_msg}")
                        elif isinstance(issue, str):
                            active_blockers.append(f"Readiness Gate Blocker: {_clip(issue.strip(), 160)}")
                    ib = rdata.get("integration_blockers") or []
                    for b in ib:
                        b_str = _clip(str(b).strip(), 140)
                        if b_str and not any(b_str in ab for ab in active_blockers):
                            active_blockers.append(f"Integration Blocker: {b_str}")
                    for bwo in (rdata.get("blocked_wo_ids") or []):
                        if not any(f"[{bwo}]" in ab for ab in active_blockers):
                            active_blockers.append(f"Blocked Work Order: {bwo}")
                    for fwo in (rdata.get("failed_wo_ids") or []):
                        if not any(f"[{fwo}]" in ab for ab in active_blockers):
                            active_blockers.append(f"Failed Work Order: {fwo}")
            except Exception:
                pass

    # 3. Check for QA verdict defect reports in inbox
    inbox_claude = ws / ".sync" / "inbox" / "claude"
    if inbox_claude.is_dir():
        for vf in sorted(inbox_claude.glob("*-qa-verdict.md")):
            try:
                v_text = vf.read_text(encoding="utf-8").strip()
                v_first = next((ln.strip() for ln in v_text.splitlines() if ln.strip()), "")
                if v_first and not any(vf.stem in b for b in active_blockers):
                    active_blockers.append(f"QA Defect Finding ({vf.name}): {_clip(v_first, 140)}")
            except Exception:
                pass

    # 4. Build specialist agent team status lines
    agent_status_lines: list[str] = []

    for role_label, agent_id, role_desc in roles_def:
        assigned = [
            wid for wid, d in wo_map.items()
            if agent_id in [str(a).lower() for a in (d.get("assigned_agents") or [])]
        ]
        if not assigned:
            if role_label == "Architecture":
                agent_status_lines.append(f"- {role_label} ({agent_id}): Active / Orchestrating & Resolving Blockers")
            else:
                agent_status_lines.append(f"- {role_label} ({agent_id}): Idle (no tasks currently assigned)")
            continue

        blocked = [
            wid for wid in assigned
            if (wo_map[wid].get("_bucket") in ("BLOCKED", "FAILED")
                or str(wo_map[wid].get("status", "")).upper() in ("BLOCKED", "FAILED"))
            and str(wo_map[wid].get("status", "")).upper() != "CANCELLED"
        ]
        active = [
            wid for wid in assigned
            if wo_map[wid].get("_bucket") == "ACTIVE"
            and str(wo_map[wid].get("status", "")).upper() not in ("CANCELLED", "COMPLETED", "BLOCKED", "FAILED")
        ]
        completed = [
            wid for wid in assigned
            if wo_map[wid].get("_bucket") == "COMPLETED"
            or str(wo_map[wid].get("status", "")).upper() == "COMPLETED"
        ]
        cancelled = [
            wid for wid in assigned
            if str(wo_map[wid].get("status", "")).upper() == "CANCELLED"
        ]

        if blocked:
            reasons = []
            for b_id in blocked:
                b_data = wo_map[b_id]
                err = b_data.get("error") or b_data.get("blocked_reason") or "execution blocked"
                short_err = _clip(str(err).strip(), 120)
                reasons.append(f"{b_id}: {short_err}")
                active_blockers.append(f"[{b_id}] ({agent_id}): {short_err}")
            agent_status_lines.append(
                f"- {role_label} ({agent_id}): BLOCKED on {', '.join(reasons)}"
            )
        elif active:
            titles = [f"{wid} ({_clip(wo_map[wid].get('title', ''), 60)})" for wid in active]
            if latest_phase in ("BLOCKED", "FAILED"):
                agent_status_lines.append(
                    f"- {role_label} ({agent_id}): HALTED / WAITING (Supervisor is {latest_phase}; tasks on hold: {', '.join(titles)})"
                )
            else:
                agent_status_lines.append(f"- {role_label} ({agent_id}): ACTIVE executing {', '.join(titles)}")
        elif completed:
            agent_status_lines.append(
                f"- {role_label} ({agent_id}): COMPLETED all assigned tasks ({', '.join(completed)}) | Ready"
            )
        elif cancelled:
            agent_status_lines.append(
                f"- {role_label} ({agent_id}): Idle (prior tasks superseded/cancelled: {', '.join(cancelled)})"
            )
        else:
            agent_status_lines.append(f"- {role_label} ({agent_id}): Idle / Waiting")

    # 5. Build Work Orders Board rows
    wo_rows: list[str] = []
    sorted_wo_ids = sorted(
        wo_map.keys(),
        key=lambda x: (
            0 if wo_map[x].get("_bucket") == "ACTIVE"
            else 1 if wo_map[x].get("_bucket") in ("BLOCKED", "FAILED")
            else 2 if wo_map[x].get("_bucket") == "COMPLETED"
            else 3
        ),
    )
    for wid in sorted_wo_ids[:limit]:
        if wid == current_wo:
            continue
        data = wo_map[wid]
        bucket = data.get("_bucket", "ACTIVE")
        status = data.get("status") or bucket
        agents = ", ".join(str(a) for a in data.get("assigned_agents") or []) or "unassigned"
        deliverable = data.get("deliverable")
        path_value = _norm(deliverable.get("path")) if isinstance(deliverable, dict) else ""
        deps = ", ".join(str(d) for d in data.get("dependencies") or []) or "none"
        header = f"- {wid} [{status}] owner={agents} deliverable={path_value or 'undeclared'} deps={deps}"
        lines = [header, f"  title: {_clip(data.get('title'), 120)}"]
        err_msg = data.get("error") or data.get("blocked_reason")
        if err_msg and isinstance(err_msg, str) and err_msg.strip():
            lines.append(f"  blocker: {_clip(err_msg.strip(), 160)}")
        wo_rows.append("\n".join(lines))

    # Assemble comprehensive Markdown block
    parts: list[str] = [
        "================================================================================",
        "TEAM STATUS & WORK ORDER BOARD (Live Supervisor Overview):",
    ]

    if supervisor_status_lines:
        parts.extend([
            "",
            "SUPERVISOR ORCHESTRATION STATUS:",
            "\n".join(supervisor_status_lines),
        ])

    parts.extend([
        "",
        "SPECIALIST AGENT TEAM STATUS:",
        "\n".join(agent_status_lines),
    ])

    if active_blockers:
        parts.extend([
            "",
            "ACTIVE BLOCKERS & DEFECT FINDINGS:",
            "\n".join(f"- {b}" for b in active_blockers[:10]),
        ])

    parts.extend([
        "",
        "WORK ORDERS LIFECYCLE BOARD:",
        "\n".join(wo_rows) if wo_rows else "- (no peer work orders)",
        "",
        "LEAD ARCHITECT DIRECTIVE:",
        "As Lead Architect, you oversee this team, the orchestration, and its deliverables.",
        f"CRITICAL CURRENT STATE: Supervisor orchestration is {latest_phase}." if latest_phase else "Supervisor orchestration status: Active",
        "When the user asks for status, 'what happened', 'is it working', or why the run is blocked or waiting:",
        "- Provide a completely honest, accurate, grounded status read from SUPERVISOR ORCHESTRATION STATUS and ACTIVE BLOCKERS.",
        "- NEVER claim 'the orchestration is working', 'nothing broke', or that specialists are actively building when supervisor is BLOCKED or FAILED.",
        "- State clearly what phase the run is in, the exact readiness issues or errors that caused it to block, and what was affected.",
        "- When the run is stalled (IDLE_STALL), waiting (WAITING_FOR_HUMAN), or blocked by dependency/order issues:",
        "  * DO NOT simply instruct the user to run :resume without fixing the underlying blocker.",
        "  * If dependencies are circular, inverted (e.g. worker depending on GitOps), or referencing missing tasks, :resume will immediately re-stall.",
        "  * YOU HAVE FULL AUTHORITY TO DIRECTLY REPAIR WORK ORDERS in .sync/work-orders/ACTIVE/ and .sync/work-orders/INDEX.yaml using write_file.",
        "  * Fix the invalid dependencies, update the YAML, explain what was unblocked, and THEN instruct the operator to run :resume.",
        "When blockers occur or specialist agents are blocked, YOU ARE EMPOWERED TO DIRECTLY",
        "AUTHOR RESOLUTION FIXES using write_file or apply_patch to repair deliverables,",
        "reconfigure work orders, align integration hooks, or fix broken tests and unblock the run.",
        "================================================================================",
    ])

    return "\n".join(parts)


def compile_worker_task_contract(
    task: Any,
    wo_data: dict[str, Any] | None,
    contract_data: dict[str, Any] | None,
    workspace: Path | str,
) -> dict[str, Any]:
    """Compile the structured task contract for one worker turn."""
    scope_entries: list[dict[str, Any]] = []
    if isinstance(contract_data, dict):
        scope = contract_data.get("scope")
        if isinstance(scope, dict):
            raw_allow = scope.get("allow") or []
            scope_entries = [r for r in raw_allow if isinstance(r, dict)]
    budget_block = (contract_data or {}).get("budget") if isinstance(contract_data, dict) else None
    file_budget = int(budget_block.get("max_files_touched", 0)) if isinstance(budget_block, dict) else 0

    deliverable = _norm(getattr(task, "deliverable_path", None))
    peers = collect_peer_deliverables(workspace, getattr(task, "work_order_id", None))
    peer_files = sorted({p for _wo, p in peers if not _is_glob(p) and p != deliverable})
    peer_prefixes = sorted({p.rstrip("/") + "/" for _wo, p in peers if _is_glob(p) or p.endswith("/")})

    exact_scope_files = sorted(
        {
            _norm(e.get("module") or e.get("path") or e.get("target"))
            for e in scope_entries
            if not _is_glob(_norm(e.get("module") or e.get("path") or e.get("target")))
        }
        - {deliverable}
        - set(peer_files)
    )
    scope_globs = sorted(
        {
            _norm(e.get("module") or e.get("path") or e.get("target"))
            for e in scope_entries
            if _is_glob(_norm(e.get("module") or e.get("path") or e.get("target")))
        }
    )

    acceptance = [
        str(c) for c in (wo_data or {}).get("acceptance_criteria") or []
        if isinstance(c, str) and c.strip()
    ][:15]
    description = str((wo_data or {}).get("description") or getattr(task, "body", "") or "").strip()
    if len(description) > 2500:
        description = description[:2500] + " …"

    turn_instructions = ""
    task_body = getattr(task, "body", "") or ""
    if "\n\nTurn Instructions:\n" in task_body:
        _, _, turn_instructions = task_body.partition("\n\nTurn Instructions:\n")
        turn_instructions = turn_instructions.strip()
    elif task_body and task_body.strip() != str((wo_data or {}).get("description", "")).strip():
        # If task_body contains unique instructions not in the work order description, treat them as turn instructions
        turn_instructions = task_body.strip()

    cfg_file = Path(workspace) / ".sync" / "config.yaml"
    env = "development"
    if cfg_file.is_file():
        try:
            cfg_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
            if isinstance(cfg_data, dict) and cfg_data.get("environment"):
                env = str(cfg_data["environment"]).lower().strip()
        except Exception:
            pass

    result: dict[str, Any] = {
        "work_order": {
            "id": getattr(task, "work_order_id", None),
            "title": str((wo_data or {}).get("title") or getattr(task, "title", "")),
            "description": description,
        },
        "project_environment": env,
        "your_deliverable": {
            "path": deliverable or None,
            "must_exist_after_your_turn": bool(deliverable),
            "content": "complete final file content, written in one write_file call",
        },
        "read_only_files": peer_files,
        "read_only_prefixes": peer_prefixes,
        "allowed_extra_paths": exact_scope_files,
        "scope_globs": scope_globs,
        "file_budget": {
            "max_files_touched": file_budget or None,
            "rule": (
                f"At most {file_budget} distinct files may be written this turn, including "
                "your deliverable. Do NOT create scaffolding, placeholder, or .gitkeep files."
                if file_budget else "Stay within your contract scope."
            ),
        },
        "acceptance_criteria": acceptance,
        "on_failure": (
            "If write_file fails, retry it once; if it fails again, stop and return "
            "status 'blocked' with a blockers entry describing the failure."
        ),
    }
    amendments = [
        {
            "revision": a.get("revision") if isinstance(a.get("revision"), int) else None,
            "agent": _safe_clip(a.get("agent"), 40),
            "reason": _safe_clip(a.get("reason"), 300),
            "changed_fields": [
                _safe_clip(field, 60)
                for field in (a.get("changed_fields") or [])[:10]
                if isinstance(field, str) and field.strip()
            ],
        }
        for a in (wo_data or {}).get("amendments") or []
        if isinstance(a, dict) and str(a.get("reason") or "").strip()
    ][-3:]
    if amendments:
        result["architect_amendments"] = amendments
        result["revision"] = (wo_data or {}).get("revision") or 1

    stale = (wo_data or {}).get("evidence_stale")
    if isinstance(stale, dict) and stale:
        result["evidence_status"] = {
            "stale": True,
            "revision": stale.get("revision"),
            "invalidated_at": _safe_clip(stale.get("invalidated_at"), 40),
            "reason": _safe_clip(stale.get("reason"), 200),
            "fields": [
                _safe_clip(field, 60)
                for field in (stale.get("fields") or [])[:10]
                if isinstance(field, str) and field.strip()
            ],
        }

    assigned_skills = list((wo_data or {}).get("skills") or (contract_data or {}).get("skills") or [])
    if assigned_skills:
        result["assigned_skills"] = assigned_skills

    plan_file = Path(workspace) / "PLAN.md"
    if plan_file.is_file():
        try:
            plan_text = plan_file.read_text(encoding="utf-8").strip()
            if plan_text:
                if len(plan_text) > 600:
                    plan_text = plan_text[:600] + " …"
                result["project_plan_overview"] = plan_text
        except Exception:
            pass

    brand_file = Path(workspace) / "brand-kit.html"
    if brand_file.is_file():
        result["brand_reference"] = "brand-kit.html exists in workspace root; inspect with read_file for brand tokens, fonts, and colors." 
    import sys
    host_os = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
    result["host_operating_system"] = host_os
    result["host_shell_environment"] = "powershell / cmd.exe" if sys.platform == "win32" else "posix / bash"

    role_str = str(getattr(task, "role", "") or (contract_data or {}).get("identity", {}).get("role", "")).lower()
    agent_id = str(getattr(task, "agent_id", "") or (contract_data or {}).get("agent_id", "")).lower()
    if agent_id in ("gemma", "qa") or role_str in ("qa", "quality"):
        result["qa_read_accessibility"] = (
            "You have full read-only access to all workspace application code and directories "
            "(e.g. src/, server/, css/, index.html) via read_file and list_directory "
            "to inspect implementation details and craft accurate test assertions. "
            "Write access remains strictly scoped to tests/."
        )

    if turn_instructions:
        result["critical_turn_instructions"] = turn_instructions
    return result


def render_worker_system(contract: dict[str, Any]) -> str:
    """Compact worker system preamble: role, non-negotiables, output contract."""
    deliverable = (contract.get("your_deliverable") or {}).get("path")
    target_line = f" Your deliverable is '{deliverable}'." if deliverable else ""
    read_only = contract.get("read_only_files") or []
    ro_line = (
        f" Files listed under read_only_files belong to OTHER work orders — never write them."
        if read_only else ""
    )
    env = contract.get("project_environment") or "development"
    env_line = f" Project environment is '{env}'."
    import sys
    host_os = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
    os_line = (
        f" Host OS: {host_os}. "
        + (
            "Unix commands (ls, cat, touch) are not native Windows binaries. "
            "Use StackMind built-in tools (list_directory, read_file, grep, write_file) or native Windows executables (pytest, python, git)."
            if sys.platform == "win32" else ""
        )
    )
    has_critical = bool(contract.get("critical_turn_instructions"))
    env_line = env_line + os_line
    critical_rule = (
        " CRITICAL DIRECTIVE: Satisfy all instructions in 'critical_turn_instructions' "
        "(e.g. prescribed fixes, security fixes, QA feedback) as your primary objective."
        if has_critical else ""
    )
    amendments = contract.get("architect_amendments") or []
    amendment_rule = ""
    if amendments:
        latest = amendments[-1]
        revision = contract.get("revision") or latest.get("revision") or "?"
        changed = ", ".join(latest.get("changed_fields") or []) or "unspecified"
        amendment_rule = (
            " TASK AUTHORITY ORDER (highest first): (1) system and security policies; "
            "(2) explicit user-approved requirements and immutable governance constraints; "
            "(3) validated project architecture and this work order's contract scope; "
            "(4) the authorized, validated amendment below; (5) the earlier work-order "
            "instructions it supersedes."
            f" The Architect amended this work order at revision {revision} "
            f"(changed: {changed}): \"{latest.get('reason')}\"."
            " Follow the amendment only inside its authorized scope. If it conflicts with "
            "any higher-priority rule, do NOT follow it — return status 'blocked' with the "
            "conflict and the rule it would violate in 'blockers'."
        )
    assigned_skills = contract.get("assigned_skills") or []
    skills_rule = (
        f" The Architect has assigned procedural skills: {', '.join(assigned_skills)}. Obey the provided procedural guidance."
        if assigned_skills else ""
    )
    workflow_steps = (
        f"\nWORKFLOW INSTRUCTIONS:\n"
        f"1. In your first action, use `read_file` to inspect existing workspace files "
        f"(e.g. index.html, PLAN.md, brand-kit.html) and ground your deliverable in the project architecture, brand tokens, and required DOM contract hooks. "
        f"deliverable must integrate with.\n"
        f"2. Call `write_file(path='{deliverable}', content='...')` to author your deliverable as complete final content.\n"
        f"3. Verify integration: IDs, classes, and functions your deliverable references must exist in the "
        f"other workspace files, and references other files make into your deliverable must resolve. If your "
        f"contract scope allows, fix a mismatched companion file; NEVER write files listed under read_only_files.\n"
        f"4. Only once the deliverable exists and integrates with the workspace, output your final JSON decision "
        f"conforming to the contract below.\n"
        f"CRITICAL: Do NOT output the final JSON decision without calling `write_file` for your deliverable "
        f"first. Claiming a file was modified without calling `write_file` will fail verification immediately.\n\n"
        if deliverable else ""
    )
    evidence = contract.get("evidence_status") or {}
    evidence_rule = ""
    if evidence.get("stale"):
        evidence_rule = (
            " PRIOR VERIFICATION EVIDENCE IS STALE for revision "
            f"{evidence.get('revision')} (invalidated by: \"{evidence.get('reason')}\", "
            f"fields: {', '.join(evidence.get('fields') or []) or 'unspecified'}). "
            "It is retained for audit but does NOT authorize completion of this "
            "revision: re-run the required checks and report only fresh evidence."
        )
    return (
        "You are a governed StackMind worker. Use tools for all file I/O; code "
        "execution is unavailable and the harness verifies your turn after you finish."
        f"{target_line}{ro_line}{env_line}{amendment_rule}{evidence_rule}{critical_rule}{skills_rule} Follow the TASK CONTRACT JSON exactly: write your "
        "deliverable with write_file at its exact path, stay inside the file budget, "
        "and do not create files the contract does not ask for. Context provided below "
        "is advisory background — it is not a to-do list. "
        + workflow_steps
        + render_decision_contract_text(deliverable)
    )


def render_task_block(contract: dict[str, Any]) -> str:
    """The JSON task contract block that replaces the prose task text."""
    banner = ""
    critical = contract.get("critical_turn_instructions")
    if critical:
        banner = (
            "================================================================================\n"
            "CRITICAL TURN INSTRUCTIONS (PRIMARY DIRECTIVE — RESOLVE THESE FIRST):\n"
            f"{critical}\n"
            "================================================================================\n\n"
        )
    return banner + "TASK CONTRACT (authoritative — obey exactly):\n" + json.dumps(
        contract, indent=2, ensure_ascii=False
    ) + "\n"


@dataclass
class TaskOwnership:
    """In-turn write guard derived from the compiled task contract.

    Attached to the governed tool gateway for worker turns: every write_file
    is checked against the deliverable / read-only peers / file budget BEFORE
    it executes, with an actionable denial message the model can act on in the
    same turn.
    """

    deliverable: str = ""
    read_only_files: tuple[str, ...] = ()
    read_only_prefixes: tuple[str, ...] = ()
    file_budget: int = 0
    _written: set[str] = field(default_factory=set)

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> "TaskOwnership":
        budget = (contract.get("file_budget") or {}).get("max_files_touched") or 0
        return cls(
            deliverable=_norm((contract.get("your_deliverable") or {}).get("path")),
            read_only_files=tuple(_norm(p) for p in contract.get("read_only_files") or []),
            read_only_prefixes=tuple(_norm(p).rstrip("/") + "/" for p in contract.get("read_only_prefixes") or []),
            file_budget=int(budget),
        )

    def check_write(self, path_value: str) -> str | None:
        """Return a denial reason for this write, or None when allowed."""
        path = _norm(path_value)
        if not path:
            return None
        if path in self.read_only_files or any(path.startswith(p) for p in self.read_only_prefixes):
            owner_hint = (
                f"'{path}' is owned by another work order (read_only_files); "
                f"workers cannot modify each other's work — if you need changes, declare this in your blockers for Architecture to coordinate. "
                f"Your deliverable is '{self.deliverable or 'unassigned'}' — write that instead."
            )
            return owner_hint
        if path not in self._written:
            if self.file_budget and len(self._written) >= self.file_budget:
                return (
                    f"file budget exceeded: {len(self._written)} of max {self.file_budget} files "
                    "already written this turn. Do not write more files; finish your deliverable "
                    f"'{self.deliverable}' with what you have and return the decision."
                )
            self._written.add(path)
        return None


def reconcile_modified_files(
    decision: Any, observed_files: tuple[str, ...] | list[str],
) -> tuple[Any, list[str]]:
    """Make the write_file journal authoritative for modified_files.

    Files the model wrote but forgot to declare are added automatically
    (reported downstream honestly); claimed-but-not-written files are NOT
    papered over — that invariant stays with the declaration-matches gate.
    Returns (possibly replaced decision, auto_added_paths).
    """
    declared = {p.replace("\\", "/").lstrip("/") for p in (decision.modified_files or ())}
    observed = {p.replace("\\", "/").lstrip("/") for p in observed_files}
    auto_added = sorted(observed - declared)
    if not auto_added:
        return decision, []
    merged = tuple(sorted(declared | observed))
    return replace(decision, modified_files=merged), auto_added


__all__ = [
    "DECISION_CONTRACT_TEXT",
    "DECISION_STATUS_ENUM",
    "TaskOwnership",
    "collect_peer_deliverables",
    "compile_worker_task_contract",
    "reconcile_modified_files",
    "render_task_block",
    "render_worker_system",
    "render_work_order_board",
]
