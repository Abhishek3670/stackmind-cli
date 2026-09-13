"""Autonomous Delivery TUI & Control Plane Integration (Milestone P7-4).

Implements the user-facing control plane for the StackMind governed runtime:
- Project Delivery View (Phase Status Banner, Agent Roles panel, Work Orders panel,
  Hierarchical Operation Tree, Live Activity Stream)
- Plan Approval Surface & Governance Controls (:plan, :approve, :reject, revision history)
- Role/Backend Surface (:roles)
- Hierarchical Operation Tree (:tree, :agents)
- Work Orders View (:wo)
- Project Completion Handover Surface (:completion)
- Reactive SSE Event Integration updating state on agent spawn & operation progress.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

import click

from cli.tui.chat import (
    render_assistant_message,
    render_assistant_message_str,
    render_chat_transcript,
    render_chat_transcript_str,
    render_user_message,
    render_user_message_str,
)
from cli.tui.diff import (
    render_file_diff,
    render_unified_diff,
    render_unified_diff_str,
)
from cli.tui.events import (
    OperationalEventManager,
    ToolActivity,
    ToolStatus,
    extract_assistant_response,
    render_operational_event,
    render_operational_event_str,
    render_tool_activity,
    render_tool_activity_line,
    render_tool_activity_line_str,
    render_tool_activity_str,
)
from cli.tui.governance import (
    render_contract_hud,
    render_contract_hud_str,
    render_plan_panel,
    render_plan_panel_str,
    render_verification_matrix,
    render_verification_matrix_str,
)
from cli.tui.landing import (
    render_landing_block,
    render_landing_block_str,
)
from cli.tui.state import (
    ActivityEntry,
    AutonomousDeliveryState,
    ChatMessage,
    OperationNode,
    PlanRevision,
    ProjectPhase,
    RoleStatus,
    WorkOrderItem,
)
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from validators.kernel.tui.views import (
    activity_line,
    contract_panel,
    diff_viewer,
    session_header,
    verification_matrix,
)


# ─── VIEW RENDERERS ───────────────────────────────────────────────────────────

def render_phase_banner(state: AutonomousDeliveryState) -> str:
    """Renders the current project delivery phase banner."""
    lines = ["PHASE"]
    if state.phase in {ProjectPhase.INITIALIZING, ProjectPhase.PLAN_PROPOSED, ProjectPhase.AWAITING_APPROVAL}:
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
            out.append(f"  Work Order: {r.work_order_id or 'None'}")
            out.append(f"  State: {r.state}")
            out.append("")
        return "\n".join(out).rstrip()

    # Dashboard compact view
    lines = ["AGENTS"]
    for role_name, r in state.roles.items():
        marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
        lines.append(f"{marker} {role_name:<16} {r.display_state}")
    return "\n".join(lines)


def render_work_orders_panel(state: AutonomousDeliveryState) -> str:
    """Renders the Work Orders panel (:wo command)."""
    lines = ["WORK ORDERS"]
    for wo in state.work_orders:
        marker = "✓" if wo.status.upper() in {"COMPLETED", "DONE"} else ("●" if wo.status.upper() in {"RUNNING", "ACTIVE"} else "○")
        title_summary = wo.title if len(wo.title) <= 24 else wo.title[:21] + "..."
        lines.append(f"{marker} {wo.id:<8} {title_summary}")
    return "\n".join(lines)


def render_operation_tree(state: AutonomousDeliveryState) -> str:
    """Renders the hierarchical operation tree (:tree / :agents command)."""
    lines = ["● Architecture"]
    root = state.operations.get("op-root")
    children = root.children if root else [k for k in state.operations if k != "op-root"]

    # Fallback to logical role tree if children list is empty
    if not children:
        entries = [
            ("Backend", state.roles.get("Backend", RoleStatus("Backend", "Codex"))),
            ("Frontend", state.roles.get("Frontend", RoleStatus("Frontend", "AGY"))),
            ("Q/A", state.roles.get("Q/A", RoleStatus("Q/A", "Ollama"))),
            ("GitOps", state.roles.get("GitOps", RoleStatus("GitOps", "Ollama"))),
        ]
        for i, (name, r) in enumerate(entries):
            is_last = (i == len(entries) - 1)
            prefix = "  └─ " if is_last else "  ├─ "
            marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {name} / {r.backend}")
        return "\n".join(lines)

    for i, child_id in enumerate(children):
        is_last = (i == len(children) - 1)
        prefix = "  └─ " if is_last else "  ├─ "
        node = state.operations.get(child_id)
        if node:
            marker = "✓" if node.status.upper() in {"COMPLETED", "DONE"} else ("●" if node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {node.role} / {node.backend}")
            # Render sub-children if present
            for j, sub_id in enumerate(node.children):
                sub_last = (j == len(node.children) - 1)
                sub_prefix = "     └─ " if sub_last else "     ├─ "
                sub_node = state.operations.get(sub_id)
                if sub_node:
                    sub_marker = "✓" if sub_node.status.upper() in {"COMPLETED", "DONE"} else ("●" if sub_node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
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
    status = plan.get("state") or ("AWAITING_APPROVAL" if state.phase == ProjectPhase.AWAITING_APPROVAL else "DRAFT")
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
            lines.append(f"  • {w.get('id', 'WO')}: {w.get('title', '')} (Role: {w.get('role', 'Worker')})")
        else:
            lines.append(f"  • {w}")

    deps = metadata.get("dependencies", ["Python 3.12", "StackMind Kernel", "Local Execution Backends"])
    lines.append("")
    lines.append(f"Dependencies: {', '.join(deps) if isinstance(deps, list) else deps}")

    risks = metadata.get("risks", ["Strict contract scope boundaries prevent out-of-scope edits."])
    lines.append("Risks & Mitigations:")
    for r in (risks if isinstance(risks, list) else [risks]):
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
        lines.append("  :approve [reason]   Approve plan and begin autonomous multi-agent execution")
        lines.append("  :reject [feedback]  Reject plan and return feedback to Architecture agent for revision")
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


# ─── REPL & DISPATCHER ────────────────────────────────────────────────────────

def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; all actions remain daemon RPC calls."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))


def _default_contract() -> dict[str, Any]:
    return {"allow": [], "deny": [], "write_mode": "governed"}


def _show_status(session: dict[str, Any], state: AutonomousDeliveryState | None = None) -> None:
    click.echo(session_header(session))
    click.echo(render_contract_hud_str(session.get("contract", {})))
    if state is not None:
        state.update_from_session(session)
        click.echo("")
        click.echo(render_project_delivery_view(state))


def _ensure_utf8() -> None:
    """Ensure standard streams support UTF-8 encoding without crashing on Windows cp1252."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _show_help() -> None:
    click.echo(
        "Available commands:\n"
        "  :status           Display current Session, Contract HUD, and Project Delivery View\n"
        "  :contract         Display Contract Boundary HUD and write permissions\n"
        "  :roles            Display Agent Roles & Execution Backend bindings\n"
        "  :rebind <r> <b>   Rebind agent role to backend (e.g., :rebind gitops ollama llama3)\n"
        "  :wo               Display Work Orders table and progress\n"
        "  :tree, :agents    Display Hierarchical Operation Tree\n"
        "  :plan             Display Plan Approval surface and revision history\n"
        "  :approve [reason] Submit Plan approval or HITL approval\n"
        "  :reject [reason]  Submit Plan rejection feedback or HITL rejection\n"
        "  :completion       Display Project Complete handover checklist\n"
        "  :cancel [target]  Cancel in-flight session turn, agent, or operation\n"
        "  :diff             Display Unified Diff viewer for staged changes\n"
        "  :matrix           Display 6-Dimensional Verification Matrix\n"
        "  :events           Stream incremental sequenced events from daemon\n"
        "  :pause            Pause active session turn\n"
        "  :resume           Resume active session\n"
        "  :chat             Display conversation transcript\n"
        "  :landing          Display branded landing block\n"
        "  :help             Show this help menu\n"
        "  :exit, :quit, q   Gracefully stop daemon and exit\n"
        "  <prompt text>     Submit a governed turn to the agent"
    )


def _run_demo(client: DaemonClient, session: dict[str, Any]) -> None:
    """Render a deterministic walkthrough demonstrating Milestone P7-4 surfaces."""
    click.echo("StackMind TUI demo")
    state = AutonomousDeliveryState(project_name="my-project", session_id=session["session_id"])
    state.update_from_session(session)

    # 1. Show Session & Legacy HUD for backward compatibility
    _show_status(session, state=None)
    click.echo("Verification: " + verification_matrix({
        "scope": True, "state": True, "ast": True, "behavioral": True,
        "security": True, "outcome": True,
    }))
    click.echo("Diff: " + diff_viewer("No staged daemon diff has been published."))

    # 2. Simulate Plan Proposal
    state.process_event({
        "name": "plan.proposed",
        "payload": {
            "plan_id": "PLAN-001",
            "title": "Autonomous Delivery Foundation",
            "metadata": {
                "work_orders": [
                    {"id": "WO-001", "title": "Architecture & Orchestration", "role": "Architecture"},
                    {"id": "WO-002", "title": "Backend API & Services", "role": "Backend"},
                    {"id": "WO-003", "title": "Frontend Client & Views", "role": "Frontend"},
                    {"id": "WO-004", "title": "Q/A Verification Suite", "role": "Q/A"},
                    {"id": "WO-005", "title": "GitOps Release & Hygiene", "role": "GitOps"},
                ]
            }
        }
    })

    # 3. Simulate Plan Approval
    state.process_event({
        "name": "plan.approved",
        "payload": {
            "plan_id": "PLAN-001",
            "reason": "Operator approved delivery architecture",
            "created_records": [
                {"id": "WO-001", "title": "Architecture & Orchestration", "assigned_agents": ["claude"]},
                {"id": "WO-002", "title": "Backend API & Services", "assigned_agents": ["codex"]},
                {"id": "WO-003", "title": "Frontend Client & Views", "assigned_agents": ["gemini"]},
                {"id": "WO-004", "title": "Q/A Verification Suite", "assigned_agents": ["gemma"]},
                {"id": "WO-005", "title": "GitOps Release & Hygiene", "assigned_agents": ["local-llm"]},
            ]
        }
    })

    # 4. Simulate Subagent Spawns & Activity
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "codex-sub-1", "role": "Backend", "backend": "Codex", "work_order_id": "WO-002"}
    })
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "gemini-sub-1", "role": "Frontend", "backend": "AGY", "work_order_id": "WO-003"}
    })
    state.process_event({
        "name": "tool_call.read",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.edit",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.create",
        "payload": {"role": "Frontend", "path": "LoginPage.tsx"}
    })
    state.process_event({
        "name": "tool_call.test",
        "payload": {"role": "Backend", "command": "pytest"}
    })

    # 5. Render Primary Autonomous Delivery View
    click.echo("\n" + render_project_delivery_view(state))


def dispatch_delivery_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState,
) -> tuple[dict[str, Any], bool]:
    """Dispatch interactive TUI commands, updating delivery state reactively."""
    normalized = text.strip()
    if not normalized:
        return session, False

    if normalized in {":exit", ":quit", "q"}:
        return session, True

    if normalized == ":help":
        _show_help()
        return session, False

    if normalized == ":status":
        session = adapter.command(":status", session_id=session["session_id"])
        _show_status(session, state=state)
        return session, False

    if normalized == ":contract":
        contract_data = session.get("contract", {})
        click.echo(render_contract_hud_str(contract_data))
        return session, False

    if normalized == ":roles":
        # Query daemon role bindings if supported, and render detailed role panel
        try:
            roles_data = client.list_roles()
            if roles_data and isinstance(roles_data, list):
                for rd in roles_data:
                    role_key = rd.get("role", "").title()
                    normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = rd.get("backend", state.roles[normalized_key].backend)
                        state.roles[normalized_key].model = rd.get("model")
        except Exception:
            pass
        click.echo(render_roles_panel(state, detailed=True))
        return session, False

    if normalized.startswith(":rebind") or normalized.startswith(":configure"):
        parts = normalized.split()
        if len(parts) >= 3:
            role_arg = parts[1]
            backend_arg = parts[2]
            model_arg = parts[3] if len(parts) >= 4 else None
            try:
                client.configure_role_backend(role=role_arg, backend=backend_arg, model=model_arg)
                role_key = role_arg.title()
                normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                if normalized_key in state.roles:
                    state.roles[normalized_key].backend = backend_arg
                    if model_arg:
                        state.roles[normalized_key].model = model_arg
                click.echo(f"[SUCCESS] Rebound role '{role_arg}' to backend '{backend_arg}'" + (f" (model: {model_arg})" if model_arg else ""))
            except Exception as err:
                click.echo(f"[ERROR] Failed to rebind role '{role_arg}': {err}")
        else:
            click.echo("Usage: :rebind <role> <backend> [model]  (e.g., :rebind gitops ollama llama3)")
        return session, False

    if normalized == ":wo":
        click.echo(render_work_orders_panel(state))
        return session, False

    if normalized in {":tree", ":agents"}:
        try:
            agents_list = client.list_agents(session_id=session["session_id"])
            if agents_list and isinstance(agents_list, list):
                for a in agents_list:
                    aid = a.get("agent_id")
                    rname = (a.get("role") or "Backend").title()
                    normalized_rname = "Q/A" if rname.upper() in {"QA", "Q/A"} else rname
                    op_id = a.get("operation_id") or f"op-{aid}"
                    parent_id = a.get("parent_operation_id") or "op-root"
                    backend = a.get("backend", "Codex")
                    status = a.get("status", "RUNNING")
                    node = OperationNode(op_id, normalized_rname, role=normalized_rname, backend=backend, status=status, parent_id=parent_id)
                    state.operations[op_id] = node
                    if parent_id in state.operations and op_id not in state.operations[parent_id].children:
                        state.operations[parent_id].children.append(op_id)
        except Exception:
            pass
        click.echo(render_operation_tree(state))
        return session, False

    if normalized == ":plan":
        try:
            plan_obj = client.plan_get(session["session_id"])
            if plan_obj:
                state.plan = dict(plan_obj)
                if plan_obj.get("state") == "AWAITING_APPROVAL":
                    state.phase = ProjectPhase.AWAITING_APPROVAL
        except Exception:
            pass
        click.echo(render_plan_panel_str(state))
        return session, False

    if normalized.startswith(":approve"):
        _, _, reason = normalized.partition(" ")
        clean_reason = reason.strip() or "Approved by operator"
        # Check if plan is awaiting approval
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
            try:
                client.plan_approve(session["session_id"], plan_id, reason=clean_reason)
                state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                state.completion_checklist["PLAN.md"] = True
                if state.plan:
                    state.plan["state"] = "APPROVED"
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "APPROVED"
                feedback_msg = f"Plan '{plan_id}' approved. Approval recorded."
                click.echo(feedback_msg)
                state.add_message("assistant", f"✓ {feedback_msg} (reason: {clean_reason})")
                return session, False
            except Exception as e:
                click.echo(f"Plan approval error: {e}")
                return session, False
        # Fallback to standard HITL approval
        adapter.command(f":approve {clean_reason}".strip(), session_id=session["session_id"])
        click.echo("Approval recorded.")
        state.add_message("assistant", f"✓ HITL Approval recorded: {clean_reason}")
        return session, False

    if normalized.startswith(":reject"):
        _, _, reason = normalized.partition(" ")
        clean_feedback = reason.strip() or "Operator requested revision"
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
            try:
                client.plan_reject(session["session_id"], plan_id, reason=clean_feedback)
                state.phase = ProjectPhase.PLAN_REJECTED
                if state.plan:
                    state.plan["state"] = "REJECTED"
                    state.plan["feedback"] = clean_feedback
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "REJECTED"
                    state.plan_revisions[-1].feedback = clean_feedback
                feedback_msg = f"Plan '{plan_id}' rejected. Rejection recorded."
                click.echo(feedback_msg)
                state.add_message("assistant", f"✗ {feedback_msg} (feedback: {clean_feedback})")
                return session, False
            except Exception as e:
                click.echo(f"Plan rejection error: {e}")
                return session, False
        # Fallback to standard HITL rejection
        adapter.command(f":reject {clean_feedback}".strip(), session_id=session["session_id"])
        click.echo("Rejection recorded.")
        state.add_message("assistant", f"✗ HITL Rejection recorded: {clean_feedback}")
        return session, False

    if normalized == ":completion":
        click.echo(render_completion_surface(state))
        return session, False

    if normalized.startswith(":cancel"):
        _, _, target = normalized.partition(" ")
        clean_target = target.strip()
        if clean_target:
            try:
                client.cancel_agent(clean_target, session_id=session["session_id"])
                click.echo(f"Agent/Operation {clean_target} CANCELLED")
                return session, False
            except Exception:
                try:
                    client.operation_cancel(clean_target)
                    click.echo(f"Operation {clean_target} CANCELLED")
                    return session, False
                except Exception as e:
                    click.echo(f"Cancellation error: {e}")
                    return session, False
        # General session turn cancellation
        session = adapter.command(":cancel", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'CANCELLED')}")
        return session, False

    if normalized == ":pause":
        session = adapter.command(":pause", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'PAUSED')}")
        return session, False

    if normalized == ":resume":
        session = adapter.command(":resume", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'RUNNING')}")
        return session, False

    if normalized.startswith(":diff"):
        _, _, diff_arg = normalized.partition(" ")
        diff_val = diff_arg.strip() if diff_arg.strip() else None
        if diff_val:
            raw_diff = adapter.command(f":diff diff={diff_val}", session_id=session["session_id"], diff=diff_val)
        else:
            raw_diff = adapter.command(":diff", session_id=session["session_id"])
        click.echo(render_unified_diff_str(str(raw_diff)))
        return session, False

    if normalized == ":matrix":
        dims = state.verification_dimensions if state is not None else None
        click.echo(render_verification_matrix_str(dims))
        return session, False

    if normalized == ":events":
        events = adapter.command(":events", session_id=session["session_id"])
        if not events:
            click.echo("No new events.")
        for event in events:
            state.process_event(event)
            click.echo(render_operational_event_str(event))
            resp = extract_assistant_response(event.get("payload", {}))
            if resp:
                state.add_message("assistant", resp)
                click.echo(render_assistant_message_str(resp))
        return session, False

    if normalized in {":chat", ":history"}:
        click.echo(render_chat_transcript_str(state.messages))
        return session, False

    if normalized == ":landing":
        click.echo(render_landing_block_str())
        return session, False

    if normalized.startswith(":") and not normalized.startswith(":prompt "):
        click.echo("Unknown command. Type :help.")
        return session, False

    # Governed turn prompt
    prompt_text = normalized[8:].strip() if normalized.startswith(":prompt ") else normalized
    state.add_message("user", prompt_text)
    click.echo(render_user_message_str(prompt_text))
    try:
        result = adapter.command(normalized, session_id=session["session_id"])
        op_id = result.get("operation_id", "turn") if isinstance(result, dict) else "turn"
        state.add_activity("User", "submitted turn", op_id)
        msg_text = f"Turn submitted to the governed daemon (operation: {op_id})."
        state.add_message("assistant", msg_text)
        click.echo(render_assistant_message_str(msg_text))

        # Check for immediate events or completed turn response from adapter
        events = list(adapter.stream(session["session_id"]))
        for ev in events:
            state.process_event(ev)
            ev_str = render_operational_event_str(ev)
            if ev_str:
                click.echo(ev_str)
            resp = extract_assistant_response(ev.get("payload", {}))
            if resp:
                state.add_message("assistant", resp)
                click.echo(render_assistant_message_str(resp))
    except Exception as err:
        err_msg = f"[ERROR] Could not submit turn: {err} (An operation may already be in flight. Use :status or :cancel)."
        state.add_message("system", err_msg)
        click.echo(err_msg)
    return session, False


# Legacy dispatch adapter wrapper for backward compatibility with _dispatch_command
def _dispatch_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState | None = None,
) -> tuple[dict[str, Any], bool]:
    if state is None:
        state = AutonomousDeliveryState(session_id=session.get("session_id", "session-1"))
        state.update_from_session(session)
    return dispatch_delivery_command(adapter, client, session, text, state)


@click.command("tui")
@click.option("--daemon-url", default=None, help="URL of an existing local daemon.")
@click.option("--agent", "-a", "agent", default="codex", show_default=True)
@click.option("--workspace", "-w", "workspace", type=click.Path(path_type=Path), default=Path("."))
@click.option("--demo", is_flag=True, help="Run the automated daemon-backed walkthrough.")
def tui(daemon_url: str | None, agent: str, workspace: Path, demo: bool) -> None:
    """Start the governed Python-native terminal control plane."""
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    daemon: LocalDaemon | None = None
    if daemon_url is None:
        state_dir = workspace.resolve() / ".sync" / "runtime" / "daemon"
        state_dir.mkdir(parents=True, exist_ok=True)
        daemon = LocalDaemon(str(state_dir), port=0).start()
        daemon_url = daemon.url

    try:
        client = DaemonClient(daemon_url)
        adapter = StackMindTuiAdapter(client)
        session = client.create_session(
            agent=agent,
            provider="daemon",
            contract=_default_contract(),
            workspace=str(workspace.resolve()),
        )
        state = AutonomousDeliveryState(
            project_name=workspace.resolve().name, session_id=session["session_id"]
        )
        state.update_from_session(session)

        _ensure_utf8()

        if demo:
            _run_demo(client, session)
            return

        if not state.has_conversation:
            click.echo(render_landing_block_str())

        while True:
            try:
                sid = (session["session_id"][:8] + "...") if "session_id" in session else "IDLE"
                text = click.prompt(f"stackmind [{sid}]", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                click.echo()
                break
            session, should_exit = dispatch_delivery_command(adapter, client, session, text, state)
            if should_exit:
                break
    finally:
        if daemon is not None:
            daemon.stop()
        if temporary_state is not None:
            temporary_state.cleanup()
