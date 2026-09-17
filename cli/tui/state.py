"""Delivery and session state models for the StackMind TUI.

Extracts the delivery lifecycle state, plan revision history, role execution status,
work order tracking, hierarchical operation tree, and live activity streams into a
dedicated, modular component.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ProjectPhase(str, Enum):
    INITIALIZING = "INITIALIZING"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PLAN_APPROVED = "PLAN_APPROVED"
    AUTONOMOUS_EXECUTION = "AUTONOMOUS_EXECUTION"
    PLAN_REJECTED = "PLAN_REJECTED"
    PROJECT_COMPLETE = "PROJECT_COMPLETE"


@dataclass
class PlanRevision:
    revision: int
    state: str
    feedback: str = ""
    timestamp: str = ""
    plan_id: str = ""


@dataclass
class RoleStatus:
    role: str
    backend: str = "Codex"
    model: str | None = None
    work_order_id: str | None = None
    state: str = "WAITING"  # WAITING, RUNNING, COMPLETED, CANCELLED, FAILED
    agent_id: str | None = None

    @property
    def display_state(self) -> str:
        s = self.state.upper()
        if s in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
            return "implementing" if self.role.lower() != "architecture" else "orchestrating"
        if s in {"COMPLETED", "DONE", "VERIFIED"}:
            return "completed" if self.role.lower() != "architecture" else "orchestrating"
        if s in {"CANCELLED", "CANCELED"}:
            return "cancelled"
        if s in {"FAILED", "ERROR"}:
            return "failed"
        return "waiting"


@dataclass
class WorkOrderItem:
    id: str
    title: str
    role: str = "Backend"
    priority: str = "P0"
    status: str = "WAITING"  # WAITING, RUNNING, COMPLETED, CANCELLED
    deliverable: str | None = None
    progress: str = "0%"


@dataclass
class OperationNode:
    operation_id: str
    name: str
    role: str = "Architecture"
    backend: str = "Claude"
    status: str = "RUNNING"
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class ActivityEntry:
    timestamp: str
    role: str
    action: str
    target: str = ""
    status: str = "OK"

    def format_line(self) -> str:
        t = self.timestamp.split("T")[-1][:5] if "T" in self.timestamp else self.timestamp[:5]
        target_part = f" {self.target}" if self.target else ""
        return f"{t:<5} {self.role:<10} {self.action}{target_part}"


@dataclass
class TurnAction:
    """Individual action invocation within an assistant turn (§19–§21)."""

    action_id: str
    description: str
    status: str = "RUNNING"  # RUNNING, COMPLETED, FAILED, CANCELLED
    duration_seconds: float | None = None
    error: str | None = None

    @property
    def symbol(self) -> str:
        s = self.status.upper()
        if s in {"COMPLETED", "SUCCESS", "DONE", "PASSED"}:
            return "✓"
        if s in {"FAILED", "FAILURE", "ERROR"}:
            return "×"
        if s in {"CANCELLED", "CANCELED"}:
            return "⊘"
        return "●"


@dataclass
class ActionsGroup:
    """Turn-specific Actions disclosure group (§20–§23)."""

    actions: list[TurnAction] = field(default_factory=list)
    expanded: bool = False
    active: bool = False
    turn_id: str | None = None

    def add_or_update(
        self,
        action_id: str,
        description: str = "",
        status: str = "RUNNING",
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> TurnAction:
        """Add new action or update existing invocation in-place (§21)."""
        for a in self.actions:
            if a.action_id == action_id:
                a.status = status
                if description and (not a.description or a.description == a.action_id):
                    a.description = description
                if duration_seconds is not None:
                    a.duration_seconds = duration_seconds
                if error is not None:
                    a.error = error
                return a
        act = TurnAction(
            action_id=action_id,
            description=description or action_id,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
        )
        self.actions.append(act)
        return act

    def complete_action(
        self,
        action_id: str,
        status: str = "COMPLETED",
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> TurnAction | None:
        """Complete an existing action in-place without duplicate cards (§21)."""
        for a in self.actions:
            if a.action_id == action_id:
                a.status = status
                if duration_seconds is not None:
                    a.duration_seconds = duration_seconds
                if error is not None:
                    a.error = error
                return a
        act = TurnAction(
            action_id=action_id,
            description=action_id,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
        )
        self.actions.append(act)
        return act

    def toggle(self) -> bool:
        """Toggle disclosure expansion (§22, §23)."""
        self.expanded = not self.expanded
        return self.expanded

    def expand(self) -> None:
        """Expand actions disclosure (§22)."""
        self.expanded = True

    def collapse(self) -> None:
        """Collapse actions disclosure (§22)."""
        self.expanded = False

    def handle_click(self, x: int | None = None, y: int | None = None) -> bool:
        """Handle mouse click on disclosure (approved mouse exception per §23)."""
        return self.toggle()

    @property
    def is_empty(self) -> bool:
        return len(self.actions) == 0

    @property
    def has_running(self) -> bool:
        return any(a.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"} for a in self.actions)

    @property
    def count(self) -> int:
        return len(self.actions)

    @property
    def completed_count(self) -> int:
        return sum(1 for a in self.actions if a.status.upper() in {"COMPLETED", "SUCCESS", "DONE", "PASSED"})

    def format_header_text(self) -> str:
        """Format the summary header line per §20 and §21."""
        arrow = "▾" if self.expanded else "▸"
        count = len(self.actions)
        if self.active or self.has_running:
            suffix = f"{count}"
        else:
            suffix = f"{count} completed"
        return f"{arrow} Actions · {suffix}"


@dataclass
class ChatMessage:
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: str = ""
    actions: ActionsGroup | None = None
    thinking: str | None = None
    turn_id: str | None = None


@dataclass
class ConversationScroll:
    """Manages independent vertical scroll position and activity tracking for the conversation column per §31."""

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
        """Notify that new conversation content arrived outside current viewport."""
        if not self.follow_bottom and self.scroll_offset > 0:
            self.has_new_activity = True


from cli.tui.runtime_panel import RuntimePanelScroll


class AutonomousDeliveryState:
    """Live in-memory state tracking the autonomous project delivery lifecycle."""

    def __init__(
        self,
        project_name: str = "stackmind-project",
        session_id: str = "SESSION-INIT",
        roles: dict[str, RoleStatus] | None = None,
        work_orders: list[WorkOrderItem] | None = None,
        operations: dict[str, OperationNode] | None = None,
        scroll: RuntimePanelScroll | None = None,
        conversation_scroll: ConversationScroll | None = None,
    ) -> None:
        self.project_name = project_name
        self.session_id = session_id
        self.phase: ProjectPhase = ProjectPhase.INITIALIZING
        self.plan: dict[str, Any] = {}
        self.plan_revisions: list[PlanRevision] = []
        if roles is not None:
            self.roles = dict(roles)
        else:
            self.roles = {
                "Architecture": RoleStatus("Architecture", backend="Claude", work_order_id="WO-001", state="RUNNING"),
                "Backend": RoleStatus("Backend", backend="Codex", work_order_id="WO-002", state="WAITING"),
                "Frontend": RoleStatus("Frontend", backend="AGY", work_order_id="WO-003", state="WAITING"),
                "Q/A": RoleStatus("Q/A", backend="Ollama", work_order_id="WO-004", state="WAITING"),
                "GitOps": RoleStatus("GitOps", backend="Ollama", work_order_id="WO-005", state="WAITING"),
            }
        if work_orders is not None:
            self.work_orders = list(work_orders)
        elif roles is not None:
            self.work_orders = []
        else:
            self.work_orders = [
                WorkOrderItem("WO-001", "Architecture & Orchestration", role="Architecture", priority="P0", status="RUNNING"),
                WorkOrderItem("WO-002", "Backend API & Services", role="Backend", priority="P0", status="WAITING"),
                WorkOrderItem("WO-003", "Frontend Client & Views", role="Frontend", priority="P0", status="WAITING"),
                WorkOrderItem("WO-004", "Q/A Verification Suite", role="Q/A", priority="P0", status="WAITING"),
                WorkOrderItem("WO-005", "GitOps Release & Hygiene", role="GitOps", priority="P0", status="WAITING"),
            ]
        if operations is not None:
            self.operations = dict(operations)
        elif roles is not None:
            self.operations = {}
        else:
            self.operations = {
                "op-root": OperationNode("op-root", "Architecture", role="Architecture", backend="Claude", status="RUNNING")
            }
        self.scroll: RuntimePanelScroll = scroll if scroll is not None else RuntimePanelScroll()
        self.conversation_scroll: ConversationScroll = (
            conversation_scroll if conversation_scroll is not None else ConversationScroll()
        )
        self.composer_buffer: str = ""
        self.is_typing: bool = False
        self.focus_target: str = "composer"
        self.activity_log: list[ActivityEntry] = []
        self.messages: list[ChatMessage] = []
        self.completion_checklist: dict[str, bool] = {
            "PLAN.md": False,
            "Backend": False,
            "Frontend": False,
            "Q/A": False,
            "GitOps": False,
            "README.md": False,
            "Tests": False,
            "Git": False,
        }
        self.verification_dimensions: dict[str, bool] = {
            "scope": True,
            "state": True,
            "ast": True,
            "behavioral": True,
            "security": True,
            "outcome": True,
        }
        self.is_complete: bool = False
        self.last_sequence: int = 0
        self.seen_sequences: set[int] = set()
        self.seen_message_keys: set[tuple[str, str]] = set()
        self.connection_status: str = "online"  # "online", "reconnecting", "offline"
        self.current_turn_actions: ActionsGroup = ActionsGroup(active=False)

    def now_str(self) -> str:
        return datetime.datetime.now().strftime("%H:%M")

    def add_activity(self, role: str, action: str, target: str = "", deduplicate: bool = False) -> None:
        if deduplicate:
            for entry in self.activity_log:
                if entry.role == role and entry.action == action and entry.target == target:
                    return
        self.activity_log.append(ActivityEntry(self.now_str(), role, action, target))

    def add_message(
        self,
        role: str,
        content: str,
        deduplicate: bool = False,
        *,
        actions: ActionsGroup | None = None,
        thinking: str | None = None,
        turn_id: str | None = None,
    ) -> ChatMessage:
        clean = content.strip()
        key = (role, clean)
        if deduplicate and key in self.seen_message_keys:
            for m in self.messages:
                if m.role == role and m.content.strip() == clean:
                    if actions is not None:
                        m.actions = actions
                    if thinking is not None:
                        m.thinking = thinking
                    return m
        self.seen_message_keys.add(key)

        msg_thinking = thinking
        msg_content = content
        if role == "assistant":
            from cli.tui.chat import extract_internal_reasoning, strip_internal_reasoning
            if msg_thinking is None and "<think" in content.lower():
                msg_thinking = extract_internal_reasoning(content)
            if "<think" in msg_content.lower():
                msg_content = strip_internal_reasoning(msg_content)

        msg_actions = actions
        if role == "assistant" and msg_actions is None and self.current_turn_actions and not self.current_turn_actions.is_empty:
            msg_actions = self.current_turn_actions
            msg_actions.active = False
            self.current_turn_actions = ActionsGroup(active=False)

        msg = ChatMessage(
            role=role,
            content=msg_content,
            timestamp=self.now_str(),
            actions=msg_actions,
            thinking=msg_thinking,
            turn_id=turn_id,
        )
        self.messages.append(msg)
        if hasattr(self, "conversation_scroll") and self.conversation_scroll is not None:
            self.conversation_scroll.notify_activity()
        return msg

    def scroll_conversation_up(self, lines: int = 1) -> None:
        """Scroll conversation viewport upward, detaching from live-follow (§31)."""
        self.conversation_scroll.scroll_up(lines)

    def scroll_conversation_down(self, lines: int = 1) -> None:
        """Scroll conversation viewport downward towards bottom (§31)."""
        self.conversation_scroll.scroll_down(lines)

    def scroll_conversation_to_bottom(self) -> None:
        """Jump conversation viewport to bottom and resume live-following (§31)."""
        self.conversation_scroll.scroll_to_bottom()

    def record_turn_action(
        self,
        action_id: str,
        description: str = "",
        status: str = "RUNNING",
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> TurnAction:
        """Record or update a turn action in-place (§21)."""
        if not self.current_turn_actions.active:
            self.current_turn_actions.active = True
        action = self.current_turn_actions.add_or_update(
            action_id=action_id,
            description=description,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
        )
        if self.messages and self.messages[-1].role == "assistant":
            if self.messages[-1].actions is None:
                self.messages[-1].actions = self.current_turn_actions
        return action

    def complete_turn_action(
        self,
        action_id: str,
        status: str = "COMPLETED",
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> TurnAction | None:
        """Complete a turn action in-place (§21)."""
        act = self.current_turn_actions.complete_action(
            action_id=action_id,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
        )
        if self.messages and self.messages[-1].role == "assistant":
            if self.messages[-1].actions is None:
                self.messages[-1].actions = self.current_turn_actions
        return act

    def get_latest_actions_group(self) -> ActionsGroup | None:
        """Get the actions group of the active turn or latest assistant message."""
        if self.current_turn_actions and not self.current_turn_actions.is_empty:
            return self.current_turn_actions
        for msg in reversed(self.messages):
            if msg.role == "assistant" and msg.actions and not msg.actions.is_empty:
                return msg.actions
        return None

    def toggle_actions(self, turn_index: int | None = None) -> bool:
        """Toggle actions expansion via keyboard (§22)."""
        if turn_index is not None:
            asst_msgs = [m for m in self.messages if m.role == "assistant" and m.actions]
            if 0 <= turn_index < len(asst_msgs):
                return asst_msgs[turn_index].actions.toggle()  # type: ignore[union-attr]
        group = self.get_latest_actions_group()
        if group:
            return group.toggle()
        return False

    def expand_actions(self, turn_index: int | None = None) -> None:
        """Expand actions disclosure via keyboard (§22)."""
        if turn_index is not None:
            asst_msgs = [m for m in self.messages if m.role == "assistant" and m.actions]
            if 0 <= turn_index < len(asst_msgs):
                asst_msgs[turn_index].actions.expand()  # type: ignore[union-attr]
                return
        group = self.get_latest_actions_group()
        if group:
            group.expand()

    def collapse_actions(self, turn_index: int | None = None) -> None:
        """Collapse actions disclosure via keyboard (§22)."""
        if turn_index is not None:
            asst_msgs = [m for m in self.messages if m.role == "assistant" and m.actions]
            if 0 <= turn_index < len(asst_msgs):
                asst_msgs[turn_index].actions.collapse()  # type: ignore[union-attr]
                return
        group = self.get_latest_actions_group()
        if group:
            group.collapse()

    def handle_mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        target: str = "actions",
        turn_index: int | None = None,
    ) -> bool:
        """Handle mouse click on actions disclosure (§23 approved mouse exception)."""
        if str(target).lower() in {"actions", "action", "disclosure"}:
            return self.toggle_actions(turn_index=turn_index)
        return False

    @property
    def has_conversation(self) -> bool:
        return len(self.messages) > 0

    def clear_conversation(self) -> None:
        self.messages.clear()
        self.current_turn_actions = ActionsGroup(active=False)

    def update_from_session(self, session: Mapping[str, Any]) -> None:
        if not session or not isinstance(session, Mapping):
            return
        self.session_id = str(session.get("session_id", self.session_id))
        # Check plans in session
        plans = session.get("plans")
        if isinstance(plans, Mapping) and plans:
            latest_plan = list(plans.values())[-1]
            if isinstance(latest_plan, Mapping):
                self.plan = dict(latest_plan)
                st = str(latest_plan.get("state", "")).upper()
                if st == "AWAITING_APPROVAL":
                    self.phase = ProjectPhase.AWAITING_APPROVAL
                elif st == "APPROVED":
                    self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                    self.completion_checklist["PLAN.md"] = True
                elif st == "REJECTED":
                    self.phase = ProjectPhase.PLAN_REJECTED

                created_wos = latest_plan.get("created_work_orders")
                if isinstance(created_wos, list):
                    self.sync_work_orders(created_wos)

        # Check journal operations
        journal = session.get("journal")
        if isinstance(journal, list):
            for entry in journal:
                if not isinstance(entry, Mapping):
                    continue
                op_id = entry.get("operation_id")
                if op_id and op_id not in self.operations:
                    role = entry.get("role") or self._infer_role_from_op(entry.get("operation", ""))
                    backend = entry.get("backend") or "Codex"
                    status = entry.get("status", "RUNNING")
                    parent_id = entry.get("parent_operation_id")
                    node = OperationNode(op_id, entry.get("operation", role), role=role, backend=backend, status=status, parent_id=parent_id)
                    self.operations[op_id] = node
                    if parent_id and parent_id in self.operations:
                        if op_id not in self.operations[parent_id].children:
                            self.operations[parent_id].children.append(op_id)
                    else:
                        if op_id not in self.operations["op-root"].children:
                            self.operations["op-root"].children.append(op_id)

    def sync_work_orders(self, wo_records: list[dict[str, Any]]) -> None:
        if not isinstance(wo_records, list):
            return
        items = []
        for r in wo_records:
            if not isinstance(r, Mapping):
                continue
            wo_id = str(r.get("id") or r.get("wo_id") or "WO-xxx")
            title = str(r.get("title", f"Work order {wo_id}"))
            agents = r.get("assigned_agents")
            role = agents[0].title() if isinstance(agents, list) and agents else "Backend"
            priority = str(r.get("priority", "P0"))
            status = str(r.get("status", "WAITING"))
            items.append(WorkOrderItem(wo_id, title, role=role, priority=priority, status=status))
        if items:
            self.work_orders = items

    def get_agent_hierarchy(self) -> list[dict[str, Any]]:
        """Return the dynamic agent hierarchy without hard-coding the agent roster (§7, §8).

        Blends live operations and configured roles into structured entries:
        ◉ Architecture    orchestrating
          ├─ ● Backend    running
          ├─ ○ Frontend   waiting
          └─ ○ GitOps     waiting
        """
        if not self.roles and not self.operations:
            return []

        entries: list[dict[str, Any]] = []

        # 1. Build from configured roles if present
        if self.roles:
            arch_role = None
            worker_roles: list[RoleStatus] = []
            for r_name, r_obj in self.roles.items():
                if r_name.lower() == "architecture" or "arch" in r_name.lower() or "lead" in r_name.lower():
                    arch_role = r_obj
                else:
                    worker_roles.append(r_obj)

            if arch_role:
                root_obj = arch_role
                children_objs = worker_roles
            elif worker_roles:
                root_obj = worker_roles[0]
                children_objs = worker_roles[1:]
            else:
                root_obj = None
                children_objs = []

            if root_obj:
                r_stat = "orchestrating" if "arch" in root_obj.role.lower() else root_obj.display_state
                entries.append({
                    "name": root_obj.role,
                    "role": root_obj.role,
                    "backend": root_obj.backend,
                    "status": r_stat,
                    "is_root": True,
                })
            for w_obj in children_objs:
                entries.append({
                    "name": w_obj.role,
                    "role": w_obj.role,
                    "backend": w_obj.backend,
                    "status": w_obj.display_state,
                    "is_root": False,
                })
            return entries

        # 2. Build from operations if no roles defined
        if self.operations:
            root = self.operations.get("op-root")
            children_ids = root.children if root else [k for k in self.operations if k != "op-root"]
            if root:
                r_status = "orchestrating" if (root.role or "").lower() == "architecture" else (
                    "running" if str(root.status).upper() in {"RUNNING", "ACTIVE"} else str(root.status).lower()
                )
                entries.append({
                    "name": root.role or "Architecture",
                    "role": root.role or "Architecture",
                    "backend": root.backend,
                    "status": r_status,
                    "is_root": True,
                })
            for cid in children_ids:
                node = self.operations.get(cid)
                if node:
                    entries.append({
                        "name": node.name or node.role,
                        "role": node.role,
                        "backend": node.backend,
                        "status": node.status.lower(),
                        "is_root": False,
                    })
            return entries

        return entries

    def get_current_operation(self) -> dict[str, Any] | None:
        """Return the currently active running operation, or None if idle (§6, §7)."""
        for op in reversed(list(self.operations.values())):
            if str(op.status).upper() in {"RUNNING", "ACTIVE"}:
                return {
                    "name": op.name,
                    "operation_id": op.operation_id,
                    "role": op.role,
                    "backend": op.backend,
                    "status": op.status.lower(),
                }
        return None

    def populate_from_runtime(
        self,
        client: Any = None,
        session: Mapping[str, Any] | None = None,
        workspace: Any = None,
    ) -> None:
        """Populate live runtime state dynamically from daemon/session before first prompt."""
        if session:
            self.update_from_session(session)

        if client is not None:
            # 1. Fetch live roles configured in daemon
            try:
                roles_list = client.list_roles()
                if roles_list and isinstance(roles_list, list):
                    new_roles: dict[str, RoleStatus] = {}
                    for item in roles_list:
                        if not isinstance(item, Mapping):
                            continue
                        r_name = str(item.get("role") or "Worker").title()
                        normalized = "Q/A" if r_name.upper() in {"QA", "Q/A"} else r_name
                        b_end = str(item.get("backend") or "Codex")
                        m_del = item.get("model")
                        new_roles[normalized] = RoleStatus(
                            role=normalized,
                            backend=b_end,
                            model=m_del,
                            state="RUNNING" if normalized.lower() == "architecture" else "WAITING",
                        )
                    if new_roles:
                        self.roles = new_roles
            except Exception:
                pass

            # 2. Fetch live agents/operations
            try:
                sid = getattr(self, "session_id", None)
                agents_list = client.list_agents(session_id=sid)
                if agents_list and isinstance(agents_list, list):
                    for a in agents_list:
                        if not isinstance(a, Mapping):
                            continue
                        aid = a.get("agent_id") or a.get("agentId") or "agent"
                        r_name = (a.get("role") or "Backend").title()
                        normalized = "Q/A" if r_name.upper() in {"QA", "Q/A"} else r_name
                        op_id = a.get("operation_id") or a.get("operationId") or f"op-{aid}"
                        parent_id = a.get("parent_operation_id") or a.get("parentOperationId") or "op-root"
                        backend = a.get("backend", "Codex")
                        status = a.get("status", "RUNNING")
                        node = OperationNode(op_id, normalized, role=normalized, backend=backend, status=status, parent_id=parent_id)
                        self.operations[op_id] = node
                        if parent_id in self.operations and op_id not in self.operations[parent_id].children:
                            self.operations[parent_id].children.append(op_id)
            except Exception:
                pass

            # 3. Fetch active plan & work orders
            try:
                sid = getattr(self, "session_id", None)
                if sid:
                    plan_obj = client.plan_get(sid)
                    if plan_obj and isinstance(plan_obj, Mapping):
                        wos = plan_obj.get("work_orders") or plan_obj.get("created_work_orders")
                        if isinstance(wos, list):
                            self.sync_work_orders(wos)
            except Exception:
                pass

    def _infer_role_from_op(self, op_name: str) -> str:
        low = op_name.lower()
        if "arch" in low or "plan" in low:
            return "Architecture"
        if "front" in low or "ui" in low or "view" in low:
            return "Frontend"
        if "qa" in low or "test" in low:
            return "Q/A"
        if "git" in low or "release" in low:
            return "GitOps"
        return "Backend"

    def process_event(self, event: Mapping[str, Any]) -> None:
        """Process incoming sequenced daemon / SSE events to update delivery state."""
        if not event or not isinstance(event, Mapping):
            return
        if hasattr(self, "scroll") and self.scroll is not None:
            self.scroll.notify_activity()
        seq = event.get("sequence")
        if seq is not None and isinstance(seq, int):
            if seq in self.seen_sequences:
                return
            self.seen_sequences.add(seq)
            if seq > self.last_sequence:
                self.last_sequence = seq

        name = str(event.get("name", ""))
        payload_raw = event.get("payload")
        payload: Mapping[str, Any] = payload_raw if isinstance(payload_raw, Mapping) else {}

        if name == "plan.proposed":
            self.phase = ProjectPhase.AWAITING_APPROVAL
            plan_id = payload.get("plan_id", "PLAN-001")
            self.plan = dict(payload)
            rev_num = len(self.plan_revisions) + 1
            self.plan["revision"] = rev_num
            self.plan_revisions.append(
                PlanRevision(
                    revision=rev_num,
                    state="AWAITING_APPROVAL",
                    plan_id=plan_id,
                    timestamp=self.now_str(),
                )
            )
            self.add_activity("Architecture", f"proposed plan {plan_id}")

        elif name == "plan.approved":
            self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
            plan_id = payload.get("plan_id", "PLAN-001")
            self.completion_checklist["PLAN.md"] = True
            if self.plan:
                self.plan["state"] = "APPROVED"
                self.plan["approval_reason"] = payload.get("reason", "")
            if self.plan_revisions:
                self.plan_revisions[-1].state = "APPROVED"
            # Update roles to implementing
            if "Backend" in self.roles:
                self.roles["Backend"].state = "RUNNING"
            if "Frontend" in self.roles:
                self.roles["Frontend"].state = "RUNNING"
            if "Q/A" in self.roles:
                self.roles["Q/A"].state = "WAITING"
            # Sync created work orders
            created_records = payload.get("created_records") or payload.get("work_orders")
            if isinstance(created_records, list) and created_records and isinstance(created_records[0], Mapping):
                self.sync_work_orders(created_records)
            self.add_activity("Architecture", f"plan approved: {plan_id}")

        elif name == "plan.rejected":
            self.phase = ProjectPhase.PLAN_REJECTED
            plan_id = payload.get("plan_id", "PLAN-001")
            feedback = payload.get("reason", "")
            if self.plan:
                self.plan["state"] = "REJECTED"
                self.plan["feedback"] = feedback
            if self.plan_revisions:
                self.plan_revisions[-1].state = "REJECTED"
                self.plan_revisions[-1].feedback = feedback
            self.add_activity("Operator", f"plan rejected: {feedback or 'Needs revision'}")

        elif name in {"event.agentSpawned", "agent.spawned"}:
            agent_id = payload.get("agent_id") or payload.get("agentId") or "agent-sub"
            role_name = (payload.get("role") or self._infer_role_from_op(agent_id)).title()
            backend = payload.get("backend") or payload.get("executionBackend") or "Codex"
            wo_id = payload.get("work_order_id") or payload.get("workOrderId")
            parent_id = payload.get("parent_operation_id") or payload.get("parentOperationId") or "op-root"
            op_id = payload.get("operation_id") or payload.get("operationId") or f"op-{agent_id}"

            # Update roles
            normalized_role = "Q/A" if role_name.upper() in {"QA", "Q/A"} else role_name
            self.roles[normalized_role] = RoleStatus(
                normalized_role,
                backend=backend,
                work_order_id=wo_id,
                state="RUNNING",
                agent_id=agent_id,
            )

            # Update tree
            node = OperationNode(op_id, normalized_role, role=normalized_role, backend=backend, status="RUNNING", parent_id=parent_id)
            self.operations[op_id] = node
            if parent_id in self.operations and op_id not in self.operations[parent_id].children:
                self.operations[parent_id].children.append(op_id)
            elif "op-root" in self.operations and op_id not in self.operations["op-root"].children:
                self.operations["op-root"].children.append(op_id)

            self.add_activity(normalized_role, f"spawned ({backend})", wo_id or "")

        elif name in {"operation.started", "turn.started"}:
            op_name = payload.get("operation") or payload.get("name") or ""
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            wo_id = payload.get("work_order_id")
            op_id = payload.get("operation_id") or f"op-{len(self.operations)+1}"
            backend = payload.get("backend") or (self.roles[normalized_role].backend if normalized_role in self.roles else "Codex")

            # Update or register operation node
            node = OperationNode(op_id, op_name or normalized_role, role=normalized_role, backend=backend, status="RUNNING")
            self.operations[op_id] = node

            if normalized_role in self.roles:
                self.roles[normalized_role].state = "RUNNING"
                if wo_id:
                    self.roles[normalized_role].work_order_id = wo_id
            for wo in self.work_orders:
                if wo.id == wo_id or wo.role.lower() == normalized_role.lower():
                    wo.status = "RUNNING"
            self.add_activity(normalized_role, "started", op_name or (wo_id or ""))

        elif name in {"operation.completed", "turn.completed"}:
            op_name = payload.get("operation") or payload.get("name") or ""
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            wo_id = payload.get("work_order_id")
            op_id = payload.get("operation_id")

            if op_id and op_id in self.operations:
                self.operations[op_id].status = "COMPLETED"
            else:
                for op in self.operations.values():
                    if (op.role == normalized_role or (op_name and op.name == op_name)) and op.status == "RUNNING":
                        op.status = "COMPLETED"

            if normalized_role in self.roles:
                self.roles[normalized_role].state = "COMPLETED"
            for wo in self.work_orders:
                if wo.id == wo_id or wo.role.lower() == normalized_role.lower():
                    wo.status = "COMPLETED"
            # Update checklist
            if "backend" in normalized_role.lower():
                self.completion_checklist["Backend"] = True
            elif "front" in normalized_role.lower():
                self.completion_checklist["Frontend"] = True
            elif "q" in normalized_role.lower():
                self.completion_checklist["Q/A"] = True
                self.completion_checklist["Tests"] = True
            elif "git" in normalized_role.lower():
                self.completion_checklist["GitOps"] = True
                self.completion_checklist["README.md"] = True
                self.completion_checklist["Git"] = True

            self.add_activity(normalized_role, "completed", op_name or (wo_id or ""))

        elif name in {"operation.cancelled", "agent.cancelled"}:
            role = (payload.get("role") or "Agent").title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            op_id = payload.get("operation_id")
            if op_id and op_id in self.operations:
                self.operations[op_id].status = "CANCELLED"
            else:
                for op in self.operations.values():
                    if op.role == normalized_role and op.status == "RUNNING":
                        op.status = "CANCELLED"
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "CANCELLED"
            self.add_activity(normalized_role, "cancelled", payload.get("reason", ""))

        elif name.startswith("tool_call.") or name == "tool.call":
            tool_name = name.partition(".")[2] if "." in name else payload.get("tool", "tool")
            role = (payload.get("role") or "Backend").title()
            target = payload.get("path") or payload.get("file") or payload.get("command") or payload.get("symbol") or ""
            self.add_activity(role, tool_name, str(target))
            call_id = str(payload.get("call_id") or payload.get("operation_id") or f"call-{len(self.current_turn_actions.actions) + 1}")
            from cli.tui.events import format_action_description
            desc = format_action_description(tool_name, str(target))
            self.record_turn_action(call_id, desc, status="RUNNING")

        elif name == "event.toolCall":
            tool_name = payload.get("tool_name", "tool")
            role = (payload.get("role") or "Agent").title()
            args_raw = payload.get("arguments")
            args = args_raw if isinstance(args_raw, Mapping) else {}
            target = args.get("path") or args.get("file") or args.get("command") or args.get("target") or ""
            self.add_activity(role, tool_name, str(target))
            call_id = str(payload.get("call_id") or payload.get("operation_id") or f"call-{len(self.current_turn_actions.actions) + 1}")
            from cli.tui.events import format_action_description
            desc = format_action_description(tool_name, str(target))
            self.record_turn_action(call_id, desc, status="RUNNING")

        elif name == "event.toolResult":
            tool_name = payload.get("tool_name", "tool")
            role = (payload.get("role") or "Agent").title()
            status = payload.get("status", "completed")
            from cli.tui.events import normalize_status
            norm_status = normalize_status(status)
            self.add_activity(role, f"{tool_name} ({norm_status.lower()})", "")
            call_id = str(payload.get("call_id") or payload.get("operation_id") or "")
            err = str(payload.get("error")) if payload.get("error") else None
            self.complete_turn_action(call_id, status=norm_status, error=err)

        elif name in {"verification.completed", "verification.recorded"}:
            res = payload.get("result")
            if isinstance(res, Mapping):
                dims = res.get("dimensions") or res.get("verification_dimensions") or res
                for k, v in dims.items():
                    norm_k = str(k).lower().replace("_verified", "")
                    if norm_k in self.verification_dimensions:
                        self.verification_dimensions[norm_k] = bool(v)
            self.add_activity("StackMind", "recorded 6-D verification matrix")

        elif name == "project.completed" or name == "project.complete":
            self.phase = ProjectPhase.PROJECT_COMPLETE
            for k in self.completion_checklist:
                self.completion_checklist[k] = True
            self.is_complete = True
            self.add_activity("StackMind", "delivered project complete handover")


__all__ = [
    "ActionsGroup",
    "ActivityEntry",
    "AutonomousDeliveryState",
    "ChatMessage",
    "ConversationScroll",
    "OperationNode",
    "PlanRevision",
    "ProjectPhase",
    "RoleStatus",
    "TurnAction",
    "WorkOrderItem",
]
