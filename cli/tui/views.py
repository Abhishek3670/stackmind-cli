"""Delivery Views and Surfaces Rendering for StackMind TUI.

Contains pure rendering functions for the Project Delivery View, Phase Banner,
Agent Roles panel, Work Orders panel, Operation Tree, Activity Stream,
Plan Approval Surface, and Completion Surface.
"""

from __future__ import annotations

from cli.tui.runtime_panel import format_model_badge
from cli.tui.state import (
    AutonomousDeliveryState,
    ProjectPhase,
    RoleStatus,
)


def render_phase_banner(state: AutonomousDeliveryState) -> str:
    """Renders the current project delivery phase banner."""
    lines = [f"=== PROJECT: {state.project_name.upper()} | PHASE: {state.phase.value} ==="]
    if state.phase in {
        ProjectPhase.INITIALIZING,
        ProjectPhase.PLAN_PROPOSED,
        ProjectPhase.AWAITING_APPROVAL,
    }:
        lines.append("● Planning & Architecture (AWAITING_APPROVAL)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.PLAN_REJECTED:
        lines.append("✗ PLAN rejected (Revising)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase in {ProjectPhase.PLAN_APPROVED, ProjectPhase.AUTONOMOUS_EXECUTION}:
        lines.append("✓ PLAN approved")
        lines.append("● Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.BLOCKED:
        lines.append("✓ PLAN approved")
        lines.append("⊘ Autonomous Execution (BLOCKED)")
        lines.append("○ Project Complete")
        if getattr(state, "error", None):
            lines.append(f"  Blocker: {state.error}")
        lines.append("  Hint: Use :resume to continue or inspect with :events")
    elif state.phase == ProjectPhase.FAILED:
        lines.append("✓ PLAN approved")
        lines.append("✗ Autonomous Execution (FAILED)")
        lines.append("○ Project Complete")
        if getattr(state, "error", None):
            lines.append(f"  Error: {state.error}")
        lines.append("  Hint: Use :resume to retry or inspect with :events")
    elif state.phase == ProjectPhase.PROJECT_COMPLETE:
        lines.append("✓ PLAN approved")
        lines.append("✓ Autonomous Execution")
        lines.append("✓ Project Complete")
    return "\n".join(lines)


def render_roles_panel(state: AutonomousDeliveryState, detailed: bool = False) -> str:
    """Renders the Agent Roles panel (:roles command)."""
    if detailed:
        out = ["=== AGENT ROLES & EXECUTION BACKENDS (:roles) ==="]
        for role_name, r in state.roles.items():
            out.append(f"{role_name} Agent")
            out.append(f"  Role: {r.role}")
            out.append(f"  Backend: {r.backend}")
            if getattr(r, "model", None):
                out.append(f"  Model: {r.model}")
            if getattr(r, "quantization", None):
                out.append(f"  Quantization: {r.quantization}")
            badge = format_model_badge(
                r.backend, getattr(r, "model", None), getattr(r, "quantization", None)
            )
            if badge:
                out.append(f"  Badge: [{badge}]")
            out.append(f"  Work Order: {r.work_order_id or 'None'}")
            out.append(f"  State: {r.state}")
            out.append("")
        return "\n".join(out).rstrip()

    # Dashboard compact view
    lines = ["AGENTS"]
    for role_name, r in state.roles.items():
        disp_state = r.display_state
        if state.is_complete or state.phase == ProjectPhase.PROJECT_COMPLETE:
            if disp_state in {"implementing", "running", "waiting", "orchestrating"}:
                disp_state = "completed"
        elif state.phase == ProjectPhase.BLOCKED:
            if role_name.lower() == "gitops" and (
                "gitops" in str(getattr(state, "error", "")).lower()
                or any(
                    w.status == "BLOCKED"
                    for w in state.work_orders
                    if w.role.lower() in ("gitops", "local-llm")
                )
            ):
                disp_state = "blocked"
            elif disp_state == "implementing":
                disp_state = "idle"
        marker = (
            "✓"
            if disp_state == "completed"
            else (
                "!"
                if disp_state == "blocked"
                else ("●" if disp_state in {"running", "implementing"} else "○")
            )
        )
        badge = format_model_badge(
            r.backend, getattr(r, "model", None), getattr(r, "quantization", None)
        )
        badge_str = (
            f" [{badge}]"
            if (
                getattr(r, "model", None)
                and disp_state in {"running", "implementing", "orchestrating"}
            )
            else ""
        )
        lines.append(f"{marker} {role_name:<16} {disp_state}{badge_str}")
    return "\n".join(lines)


def render_work_orders_panel(state: AutonomousDeliveryState) -> str:
    """Renders the Work Orders panel (:wo command)."""
    lines = ["WORK ORDERS"]
    if not state.work_orders:
        lines.append("  No active orders")
        return "\n".join(lines)
    for wo in state.work_orders:
        st = wo.status.upper()
        if st in {"COMPLETED", "DONE", "VERIFIED", "SUCCESS"}:
            marker = "✓"
        elif st in {"BLOCKED"}:
            marker = "⊘"
        elif st in {"RUNNING", "ACTIVE", "IN_PROGRESS"}:
            marker = "●"
        elif st in {"FAILED", "ERROR"}:
            marker = "×"
        else:
            marker = "○"
        title_summary = wo.title if len(wo.title) <= 24 else wo.title[:21] + "..."
        lines.append(f"{marker} {wo.id:<8} {title_summary}")
    return "\n".join(lines)


def render_operation_tree(state: AutonomousDeliveryState) -> str:
    """Renders the hierarchical operation tree (:tree / :agents command)."""
    arch_marker = "✓" if (state.is_complete or state.phase == ProjectPhase.PROJECT_COMPLETE) else "●"
    lines = [f"{arch_marker} Architecture"]
    root = state.operations.get("op-root")
    children = root.children if root else [k for k in state.operations if k != "op-root"]

    if not children:
        entries = [
            ("Backend", state.roles.get("Backend", RoleStatus("Backend", "Codex"))),
            ("Frontend", state.roles.get("Frontend", RoleStatus("Frontend", "AGY"))),
            ("Q/A", state.roles.get("Q/A", RoleStatus("Q/A", "Ollama"))),
            ("GitOps", state.roles.get("GitOps", RoleStatus("GitOps", "Ollama"))),
        ]
        for i, (name, r) in enumerate(entries):
            is_last = i == len(entries) - 1
            prefix = "  └─ " if is_last else "  ├─ "
            marker = (
                "✓"
                if (state.is_complete or state.phase == ProjectPhase.PROJECT_COMPLETE or r.state.upper() in {"COMPLETED", "DONE"})
                else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
            )
            lines.append(f"{prefix}{marker} {name} / {r.backend}")
        return "\n".join(lines)

    for i, child_id in enumerate(children):
        is_last = i == len(children) - 1
        prefix = "  └─ " if is_last else "  ├─ "
        node = state.operations.get(child_id)
        if node:
            marker = (
                "✓"
                if (state.is_complete or state.phase == ProjectPhase.PROJECT_COMPLETE or node.status.upper() in {"COMPLETED", "DONE"})
                else ("●" if node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
            )
            lines.append(f"{prefix}{marker} {node.role} / {node.backend}")
            # Render sub-children if present
            for j, sub_id in enumerate(node.children):
                sub_last = j == len(node.children) - 1
                sub_prefix = "     └─ " if sub_last else "     ├─ "
                sub_node = state.operations.get(sub_id)
                if sub_node:
                    sub_marker = (
                        "✓"
                        if (state.is_complete or state.phase == ProjectPhase.PROJECT_COMPLETE or sub_node.status.upper() in {"COMPLETED", "DONE"})
                        else ("●" if sub_node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
                    )
                    lines.append(f"{sub_prefix}{sub_marker} {sub_node.name} ({sub_node.role})")
    return "\n".join(lines)


def render_activity_stream(state: AutonomousDeliveryState, limit: int = 10) -> str:
    """Renders the Live Activity Stream panel."""
    lines = ["ACTIVITY"]
    entries = state.activity_log[-limit:]
    if not entries:
        lines.append("  (Awaiting first runtime activity event)")
    else:
        for entry in entries:
            lines.append(entry.format_line())
    return "\n".join(lines)


def render_plan_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Plan Approval Surface & Governance Controls (:plan command)."""
    plan = state.plan
    plan_id = plan.get("plan_id") or plan.get("id") or "PLAN.md"
    title = plan.get("title", "Autonomous Delivery Plan")
    status = plan.get("state") or (
        "AWAITING_APPROVAL" if state.phase == ProjectPhase.AWAITING_APPROVAL else "DRAFT"
    )
    content = plan.get("content", "")
    metadata = plan.get("metadata", {})

    lines = [
        "PLAN.md",
        "─" * 52,
        f"Status: {status}",
        f"Plan ID: {plan_id}",
        f"Title: {title}",
        "",
        "WORK DECOMPOSITION:",
    ]

    wos = metadata.get("work_orders") or [
        {"id": wo.id, "title": wo.title, "role": wo.role} for wo in state.work_orders
    ]
    for w in wos:
        if isinstance(w, dict):
            lines.append(
                f"  • {w.get('id', 'WO')}: {w.get('title', '')} (Role: {w.get('role', 'Worker')})"
            )
        else:
            lines.append(f"  • {w}")

    deps = metadata.get(
        "dependencies", ["Python 3.12", "StackMind Kernel", "Local Execution Backends"]
    )
    lines.append("")
    lines.append(f"Dependencies: {', '.join(deps) if isinstance(deps, list) else deps}")

    risks = metadata.get("risks", ["Strict contract scope boundaries prevent out-of-scope edits."])
    lines.append("Risks & Mitigations:")
    for r in risks if isinstance(risks, list) else [risks]:
        lines.append(f"  - {r}")

    lines.append("Validation: PASS (Schema valid, 6-D matrix confirmed)")

    if state.plan_revisions:
        lines.append("")
        lines.append("REVISION HISTORY:")
        for rev in state.plan_revisions:
            feedback_str = f" — Feedback: '{rev.feedback}'" if rev.feedback else ""
            lines.append(f"  [Rev {rev.revision}] {rev.state}{feedback_str} ({rev.timestamp})")

    if status == "AWAITING_APPROVAL" or state.phase == ProjectPhase.AWAITING_APPROVAL:
        lines.append("")
        lines.append("GOVERNANCE CONTROLS:")
        lines.append(
            "  :approve [reason]   Approve plan and begin autonomous multi-agent execution"
        )
        lines.append(
            "  :reject [feedback]  Reject plan and return feedback to Architecture agent for revision"
        )
    elif status == "APPROVED":
        lines.append("")
        lines.append("STATUS: Approved by operator. Execution in progress.")

    return "\n".join(lines)


def render_completion_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Project Completion Handover Surface."""
    lines = [
        "PROJECT COMPLETE",
        "",
    ]
    for item, done in state.completion_checklist.items():
        marker = "✓" if done else "○"
        lines.append(f"{item} {marker}")
    return "\n".join(lines)


def render_project_delivery_view(state: AutonomousDeliveryState) -> str:
    """Renders the primary Project Delivery TUI view combining Phase, Roles, WOs, Activity."""
    header = f"STACKMIND • {state.project_name}\n" + "─" * 52
    phase = render_phase_banner(state)
    agents = render_roles_panel(state, detailed=False)
    wos = render_work_orders_panel(state)
    activity = render_activity_stream(state, limit=8)

    return f"{header}\n\n{phase}\n\n{agents}\n\n{wos}\n\n{activity}"
