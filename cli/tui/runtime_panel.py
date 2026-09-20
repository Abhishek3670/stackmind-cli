"""StackMind Runtime Panel — Dynamic Live State Display (Phase 4 / WO-044).

Renders the persistent right-side StackMind Runtime panel populated dynamically
from live project and daemon state per IMPLEMENTATION_PLAN_TUI.md §7-§10, §37:
- AGENTS section: Dynamic agent hierarchy from actual runtime/project data.
  Tree formatting: '◉ Architecture orchestrating', '  ├─ ● Backend running', etc.
  Standard symbols: ◉ orchestrating, ● running, ✓ completed, ○ waiting/idle,
  × failed, ⊘ cancelled, ! blocked. Never hard-codes the agent roster.
- WORK ORDERS section: Active and queued work orders from AutonomousDeliveryState.
- CURRENT OPERATION section: Compact display showing active operation, role, backend.
- Independent vertical scroll with '↓ New runtime activity' indicator.
- Event-driven updates without synthetic animation or fake intermediate states.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Mapping

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# ── Panel Constants ──────────────────────────────────────────────────────────

RUNTIME_HEADING = "StackMind Runtime"
RUNTIME_SECTIONS = ("AGENTS", "WORK ORDERS", "CURRENT OPERATION")

# Standard symbols and color styles per §8
STATUS_SYMBOLS: dict[str, tuple[str, str, str]] = {
    "orchestrating": ("◉", "bold #38bdf8", "orchestrating"),
    "running": ("●", "bold #22c55e", "running"),
    "implementing": ("●", "bold #22c55e", "implementing"),
    "active": ("●", "bold #22c55e", "running"),
    "in_progress": ("●", "bold #22c55e", "running"),
    "completed": ("✓", "bold #10b981", "completed"),
    "done": ("✓", "bold #10b981", "completed"),
    "verified": ("✓", "bold #10b981", "completed"),
    "success": ("✓", "bold #10b981", "completed"),
    "waiting": ("○", "dim #94a3b8", "waiting"),
    "queued": ("○", "dim #94a3b8", "queued"),
    "pending": ("○", "dim #94a3b8", "pending"),
    "idle": ("○", "dim #64748b", "idle"),
    "configured": ("○", "dim #64748b", "idle"),
    "failed": ("×", "bold #ef4444", "failed"),
    "error": ("×", "bold #ef4444", "failed"),
    "cancelled": ("⊘", "dim #f87171", "cancelled"),
    "canceled": ("⊘", "dim #f87171", "cancelled"),
    "blocked": ("!", "bold #eab308", "blocked"),
}


def get_status_symbol(raw_status: str | None) -> tuple[str, str, str]:
    """Resolve a raw status string to (symbol, rich_style, display_label)."""
    if not raw_status:
        return ("○", "dim #64748b", "idle")
    normalized = str(raw_status).lower().strip().replace(" ", "_")
    if normalized in STATUS_SYMBOLS:
        return STATUS_SYMBOLS[normalized]
    # Fallback heuristic
    if "run" in normalized or "prog" in normalized or "exec" in normalized:
        return ("●", "bold #22c55e", normalized)
    if "comp" in normalized or "done" in normalized or "pass" in normalized:
        return ("✓", "bold #10b981", normalized)
    if "fail" in normalized or "err" in normalized:
        return ("×", "bold #ef4444", normalized)
    if "cancel" in normalized:
        return ("⊘", "dim #f87171", normalized)
    if "block" in normalized:
        return ("!", "bold #eab308", normalized)
    return ("○", "dim #94a3b8", normalized)


# ── Independent Scroll State ─────────────────────────────────────────────────

@dataclass
class RuntimePanelScroll:
    """Manages independent vertical scroll position and activity tracking per §10."""

    scroll_offset: int = 0
    viewport_height: int | None = None
    has_new_activity: bool = False
    follow_bottom: bool = True

    def scroll_up(self, lines: int = 1) -> None:
        """Scroll view upward, detaching from live-following."""
        self.scroll_offset += max(1, lines)
        self.follow_bottom = False

    def scroll_down(self, lines: int = 1) -> None:
        """Scroll view downward towards bottom."""
        self.scroll_offset = max(0, self.scroll_offset - max(1, lines))
        if self.scroll_offset == 0:
            self.follow_bottom = True
            self.has_new_activity = False

    def scroll_to_bottom(self) -> None:
        """Jump to the bottom and resume live-following."""
        self.scroll_offset = 0
        self.follow_bottom = True
        self.has_new_activity = False

    def notify_activity(self) -> None:
        """Notify that new runtime state arrived outside current viewport."""
        if not self.follow_bottom and self.scroll_offset > 0:
            self.has_new_activity = True


# ── Formatting Helpers ───────────────────────────────────────────────────────

def format_model_badge(
    backend: str | None = None,
    model: str | None = None,
    quantization: str | None = None,
) -> str:
    """Format a compact model and quantization badge for an agent role.

    Examples:
    - backend='ollama', model='qwen2.5-coder:7b', quantization='q4_k_m' -> 'ollama/qwen2.5-coder:7b (q4_k_m)'
    - backend='ollama', model='qwen2.5-coder:7b' -> 'ollama/qwen2.5-coder:7b'
    - backend='openai', model='gpt-4o' -> 'openai/gpt-4o'
    - backend='Claude', model='claude-3-5-sonnet' -> 'Claude/claude-3-5-sonnet'
    - backend='Codex', model=None -> 'Codex'
    """
    b = str(backend).strip() if backend else ""
    m = str(model).strip() if model else ""
    q = str(quantization).strip() if quantization else ""

    if m:
        if b and not m.lower().startswith(f"{b.lower()}/"):
            main_id = f"{b}/{m}"
        else:
            main_id = m
    elif b:
        main_id = b
    else:
        main_id = ""

    if not main_id:
        return ""

    if q and q.lower() not in main_id.lower():
        return f"{main_id} ({q})"
    return main_id


def format_agent_tree(
    agents: list[dict[str, Any]] | list[Any],
    width: int | None = None,
) -> list[Text]:
    """Format agent hierarchy dynamically into tree lines with model badges.

    Outputs:
    ◉ Architecture    orchestrating
        [Claude/claude-3-5-sonnet]
      ├─ ● Backend    running
      │   [ollama/qwen2.5-coder:7b (q4_k_m)]
      ├─ ○ Frontend   waiting
      └─ ○ GitOps     waiting
    """
    if not agents:
        return [Text("  No agents", style="dim #475569")]

    # Normalize agents into dictionaries
    normalized: list[dict[str, Any]] = []
    for a in agents:
        if isinstance(a, Mapping):
            normalized.append(dict(a))
        elif hasattr(a, "role"):
            normalized.append({
                "name": getattr(a, "role", "Worker"),
                "status": getattr(a, "display_state", getattr(a, "state", "idle")),
                "role": getattr(a, "role", "Worker"),
                "backend": getattr(a, "backend", ""),
                "model": getattr(a, "model", None),
                "quantization": getattr(a, "quantization", None),
            })
        else:
            normalized.append({"name": str(a), "status": "idle"})

    # Check if a root / architecture agent exists
    root_idx = -1
    for i, a in enumerate(normalized):
        role_name = str(a.get("role") or a.get("name") or "").lower()
        if "arch" in role_name or a.get("is_root") or a.get("status") == "orchestrating":
            root_idx = i
            break

    # If no explicit architecture role found and multiple agents, pick first as root if orchestrating
    if root_idx == -1 and len(normalized) > 1 and normalized[0].get("status") == "orchestrating":
        root_idx = 0

    lines: list[Text] = []

    # Case 1: Root agent + children tree
    if root_idx != -1:
        root = normalized[root_idx]
        children = [a for i, a in enumerate(normalized) if i != root_idx]

        r_name = str(root.get("name") or root.get("role") or "Architecture")
        r_status = str(root.get("status") or "orchestrating")
        sym, style, disp = get_status_symbol(r_status)

        root_line = Text()
        root_line.append(f"{sym} ", style=style)
        root_line.append(f"{r_name:<16}", style="bold white")
        root_line.append(f" {disp}", style="dim #94a3b8")
        lines.append(root_line)

        # Render model/quantization badge for active role or configured model
        r_badge = format_model_badge(
            root.get("backend"),
            root.get("model"),
            root.get("quantization"),
        )
        is_active_root = r_status.lower() in {"orchestrating", "running", "implementing", "active", "in_progress"}
        if r_badge and (root.get("model") or is_active_root):
            max_badge_len = max(10, (width or 36) - 8)
            trunc_badge = r_badge if len(r_badge) <= max_badge_len else r_badge[:max_badge_len - 1] + "…"
            badge_line = Text()
            badge_line.append("    ")
            badge_line.append(f"[{trunc_badge}]", style="dim #a855f7")
            lines.append(badge_line)

        for j, child in enumerate(children):
            is_last = (j == len(children) - 1)
            prefix = "  └─ " if is_last else "  ├─ "
            c_name = str(child.get("name") or child.get("role") or "Worker")
            c_status = str(child.get("status") or "waiting")
            csym, cstyle, cdisp = get_status_symbol(c_status)

            child_line = Text()
            child_line.append(prefix, style="dim #475569")
            child_line.append(f"{csym} ", style=cstyle)
            child_line.append(f"{c_name:<12}", style="white")
            child_line.append(f" {cdisp}", style="dim #94a3b8")
            lines.append(child_line)

            c_badge = format_model_badge(
                child.get("backend"),
                child.get("model"),
                child.get("quantization"),
            )
            is_active_child = c_status.lower() in {"orchestrating", "running", "implementing", "active", "in_progress"}
            if c_badge and (child.get("model") or is_active_child):
                b_prefix = "      " if is_last else "  │   "
                max_badge_len = max(10, (width or 36) - len(b_prefix) - 4)
                trunc_badge = c_badge if len(c_badge) <= max_badge_len else c_badge[:max_badge_len - 1] + "…"
                badge_line = Text()
                badge_line.append(b_prefix, style="dim #475569")
                badge_line.append(f"[{trunc_badge}]", style="dim #a855f7")
                lines.append(badge_line)

    # Case 2: Flat list with symbols or simple dicts
    else:
        for item in normalized:
            name = str(item.get("name") or item.get("role") or "Worker")
            status = str(item.get("status") or "idle")
            sym, style, disp = get_status_symbol(status)

            line = Text()
            line.append("  ")
            line.append(f"{sym} ", style=style)
            line.append(f"{name}: ", style="dim white")
            line.append(f"{status}", style="dim #94a3b8")
            lines.append(line)

            f_badge = format_model_badge(
                item.get("backend"),
                item.get("model"),
                item.get("quantization"),
            )
            is_active_item = status.lower() in {"orchestrating", "running", "implementing", "active", "in_progress"}
            if f_badge and (item.get("model") or is_active_item):
                max_badge_len = max(10, (width or 36) - 8)
                trunc_badge = f_badge if len(f_badge) <= max_badge_len else f_badge[:max_badge_len - 1] + "…"
                badge_line = Text()
                badge_line.append("    ")
                badge_line.append(f"[{trunc_badge}]", style="dim #a855f7")
                lines.append(badge_line)

    return lines


def format_work_orders(
    work_orders: list[dict[str, Any]] | list[Any] | None,
    width: int | None = None,
) -> list[Text]:
    """Format work orders section into clean status lines."""
    if not work_orders:
        return [Text("  No active orders", style="dim #475569")]

    lines: list[Text] = []
    max_title_len = max(14, (width or 32) - 18)

    for wo in work_orders:
        if isinstance(wo, Mapping):
            wo_id = str(wo.get("id") or wo.get("wo_id") or "—")
            title = str(wo.get("title", ""))
            status = str(wo.get("status", "WAITING"))
        elif hasattr(wo, "id"):
            wo_id = getattr(wo, "id", "—")
            title = getattr(wo, "title", "")
            status = getattr(wo, "status", "WAITING")
        else:
            wo_id = str(wo)
            title = ""
            status = "WAITING"

        sym, style, _ = get_status_symbol(status)

        trunc_title = title if len(title) <= max_title_len else title[:max_title_len - 1] + "…"
        line = Text()
        line.append("  ")
        line.append(f"{sym} ", style=style)
        line.append(f"{wo_id}", style="bold #60a5fa")
        if trunc_title:
            line.append(f": {trunc_title}", style="dim white")
        lines.append(line)

    return lines


def format_current_operation(
    current_operation: dict[str, Any] | Any | None,
    width: int | None = None,
) -> list[Text]:
    """Format current operation section into compact display."""
    if not current_operation:
        return [Text("  ○ Idle", style="dim #475569")]

    if isinstance(current_operation, Mapping):
        op_name = str(current_operation.get("name") or current_operation.get("operation") or "—")
        op_status = str(current_operation.get("status") or "running")
        role = current_operation.get("role")
        backend = current_operation.get("backend")
    elif hasattr(current_operation, "name"):
        op_name = getattr(current_operation, "name", "—")
        op_status = getattr(current_operation, "status", "running")
        role = getattr(current_operation, "role", None)
        backend = getattr(current_operation, "backend", None)
    else:
        op_name = str(current_operation)
        op_status = "running"
        role = None
        backend = None

    sym, style, disp = get_status_symbol(op_status)
    lines: list[Text] = []

    header = Text()
    header.append("  ")
    header.append(f"{sym} ", style=style)
    if role:
        header.append(f"{role}", style="bold white")
        if backend:
            header.append(f" · {backend}", style="dim #a855f7")
    else:
        header.append(f"{op_name}", style="bold white")
    lines.append(header)

    sub = Text()
    sub.append("    ")
    if op_name and op_name != "—":
        sub.append(f"{op_name} ", style="dim white")
    sub.append(f"({disp})", style="dim #64748b")
    lines.append(sub)

    return lines


# ── Main Runtime Panel Renderer ──────────────────────────────────────────────

def render_runtime_panel(
    width: int | None = None,
    height: int | None = None,
    *,
    agents: list[dict[str, Any]] | list[Any] | None = None,
    work_orders: list[dict[str, Any]] | list[Any] | None = None,
    current_operation: dict[str, Any] | Any | None = None,
    state: Any | None = None,
    scroll: RuntimePanelScroll | None = None,
) -> RenderableType:
    """Render the persistent StackMind Runtime panel (right column).

    Populates live data per §7-§10:
    - AGENTS: dynamic agent hierarchy
    - WORK ORDERS: active and queued orders
    - CURRENT OPERATION: compact active operation
    - Independent scroll with '↓ New runtime activity' indicator
    """
    # Extract data from state if provided
    if state is not None:
        if agents is None:
            if hasattr(state, "get_agent_hierarchy"):
                raw_agents = state.get_agent_hierarchy()
                agents = []
                roles_map = getattr(state, "roles", {}) or {}
                for a in raw_agents:
                    if isinstance(a, Mapping):
                        ad = dict(a)
                        r_name = ad.get("role") or ad.get("name")
                        r_obj = roles_map.get(r_name)
                        if r_obj:
                            if not ad.get("model"):
                                ad["model"] = getattr(r_obj, "model", None)
                            if not ad.get("quantization"):
                                ad["quantization"] = getattr(r_obj, "quantization", None)
                        agents.append(ad)
                    else:
                        agents.append(a)
            elif hasattr(state, "roles"):
                agents = list(state.roles.values())
        if work_orders is None:
            if hasattr(state, "work_orders"):
                work_orders = list(getattr(state, "work_orders"))
        if current_operation is None:
            if hasattr(state, "get_current_operation"):
                current_operation = state.get_current_operation()
            elif hasattr(state, "operations"):
                running = [op for op in state.operations.values() if getattr(op, "status", "").upper() in {"RUNNING", "ACTIVE"}]
                if running:
                    current_operation = running[-1]
        if scroll is None and hasattr(state, "scroll"):
            scroll = getattr(state, "scroll")

    items: list[RenderableType] = []

    # 1. Heading
    items.append(Text(RUNTIME_HEADING, style="bold dim white", justify="center"))
    items.append(Text(""))

    # 2. Section: AGENTS
    items.append(Text("AGENTS", style="bold #38bdf8"))
    for agent_line in format_agent_tree(agents or [], width=width):
        items.append(agent_line)
    items.append(Text(""))

    # 3. Divider
    items.append(Rule(style="dim #334155"))
    items.append(Text(""))

    # 4. Section: WORK ORDERS
    items.append(Text("WORK ORDERS", style="bold #60a5fa"))
    for wo_line in format_work_orders(work_orders, width=width):
        items.append(wo_line)
    items.append(Text(""))

    # 5. Divider
    items.append(Rule(style="dim #334155"))
    items.append(Text(""))

    # 6. Section: CURRENT OPERATION
    items.append(Text("CURRENT OPERATION", style="bold #a855f7"))
    for op_line in format_current_operation(current_operation, width=width):
        items.append(op_line)

    # 7. Independent scroll indicator (§10)
    if scroll is not None and scroll.has_new_activity:
        items.append(Text(""))
        items.append(Text("↓ New runtime activity", style="bold #38bdf8", justify="center"))

    return Panel(
        Group(*items),
        box=box.SIMPLE,
        border_style="dim #334155",
        padding=(0, 1),
        width=width,
        height=height,
    )


def render_runtime_panel_str(
    width: int = 30,
    height: int | None = None,
    *,
    agents: list[dict[str, Any]] | list[Any] | None = None,
    work_orders: list[dict[str, Any]] | list[Any] | None = None,
    current_operation: dict[str, Any] | Any | None = None,
    state: Any | None = None,
    scroll: RuntimePanelScroll | None = None,
) -> str:
    """Render the runtime panel as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(
        file=buf, record=True, width=width, force_terminal=False, color_system=None
    )
    console.print(
        render_runtime_panel(
            width=width,
            height=height,
            agents=agents,
            work_orders=work_orders,
            current_operation=current_operation,
            state=state,
            scroll=scroll,
        )
    )
    return console.export_text().rstrip()


__all__ = [
    "RUNTIME_HEADING",
    "RUNTIME_SECTIONS",
    "STATUS_SYMBOLS",
    "RuntimePanelScroll",
    "format_agent_tree",
    "format_current_operation",
    "format_model_badge",
    "format_work_orders",
    "get_status_symbol",
    "render_runtime_panel",
    "render_runtime_panel_str",
]
