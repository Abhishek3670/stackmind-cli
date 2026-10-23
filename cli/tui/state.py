"""Delivery and session state models for the StackMind TUI.

Extracts the delivery lifecycle state, plan revision history, role execution status,
work order tracking, hierarchical operation tree, and live activity streams into a
dedicated, modular component.
"""

from __future__ import annotations

import datetime
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml


def canonical_blocker_text(block_msg: Any) -> str:
    """Collapse a blocker payload into one canonical display string.

    Structured evidence packets (failure_code + work order + contract revision)
    render as a single deduplicated canonical line; raw evidence remains
    available in the underlying payload instead of being repeated in the list.
    """
    if not block_msg:
        return ""
    if isinstance(block_msg, Mapping):
        failure = block_msg.get("failure")
        if isinstance(failure, Mapping):
            code = str(failure.get("failure_code") or "BLOCKED")
            wo = str(failure.get("work_order_id") or "?")
            rev = failure.get("contract_revision", 1)
            msg = str(failure.get("canonical_message") or block_msg.get("error") or "")
            return f"{code} [{wo} rev {rev}] {msg}".strip()
        from cli.tui.events import clean_error_message
        err_val = block_msg.get("error") or block_msg.get("reason") or block_msg.get("message")
        if err_val:
            m = re.search(r"\[(.*?)\]", str(err_val))
            if m:
                err_val = m.group(1).strip()
            return clean_error_message(err_val)
        return clean_error_message(block_msg)
    from cli.tui.events import clean_error_message
    return clean_error_message(block_msg)


class ProjectPhase(str, Enum):
    INITIALIZING = "INITIALIZING"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PLAN_APPROVED = "PLAN_APPROVED"
    AUTONOMOUS_EXECUTION = "AUTONOMOUS_EXECUTION"
    PLAN_REJECTED = "PLAN_REJECTED"
    PROJECT_COMPLETE = "PROJECT_COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


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
    quantization: str | None = None
    work_order_id: str | None = None
    state: str = "WAITING"  # WAITING, RUNNING, COMPLETED, CANCELLED, FAILED
    agent_id: str | None = None

    @property
    def display_state(self) -> str:
        s = self.state.upper()
        if s in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
            return "implementing" if self.role.lower() != "architecture" else "orchestrating"
        if s in {"COMPLETED", "DONE", "VERIFIED"}:
            return "completed"
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
    tool_name: str | None = None
    target: str | None = None
    output: str | None = None
    diff: str | None = None
    code: str | None = None
    parent_id: str | None = None
    role: str | None = None
    sub_actions: list[TurnAction] = field(default_factory=list)

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
    preview_label: str | None = None
    preview_code: str | None = None
    verbosity: str = "tree"  # "tree" | "flat" | "compact"

    @property
    def all_actions(self) -> list[TurnAction]:
        """Flattened list of all top-level and nested sub-actions."""
        res: list[TurnAction] = []
        for a in self.actions:
            res.append(a)
            res.extend(a.sub_actions)
        return res

    @property
    def failed_count(self) -> int:
        return sum(1 for a in self.all_actions if a.status.upper() in {"FAILED", "FAILURE", "ERROR"})

    @property
    def latest_preview_action(self) -> TurnAction | None:
        """Find the running action, or the most recent completed action with code/diff/output/error."""
        all_acts = self.all_actions
        for a in reversed(all_acts):
            if a.status.upper() == "RUNNING":
                return a
        # 1. Prefer real agent tool actions with diff, output, code, or error over internal harness plumbing
        for a in reversed(all_acts):
            t_name = (a.tool_name or a.action_id or "").lower()
            if "harness" in t_name:
                continue
            if (
                (a.diff and a.diff.strip())
                or (a.output and a.output.strip())
                or (a.code and a.code.strip())
                or (a.error and a.error.strip())
            ):
                return a
        # 2. Check any other action with diff, code, or error
        for a in reversed(all_acts):
            if (a.diff and a.diff.strip()) or (a.code and a.code.strip()) or (a.error and a.error.strip()):
                return a
        # 3. Check any non-harness action
        for a in reversed(all_acts):
            t_name = (a.tool_name or a.action_id or "").lower()
            if "harness" not in t_name:
                return a
        # 4. Fallback to completed action with output
        for a in reversed(all_acts):
            if a.output and a.output.strip():
                return a
        return all_acts[-1] if all_acts else None

    def get_visible_actions(self, verbosity: str | None = None) -> list[tuple[TurnAction, int]]:
        """Return list of (action, indent_level) according to verbosity mode."""
        mode = (verbosity or self.verbosity or "tree").lower()
        items: list[tuple[TurnAction, int]] = []
        for a in self.actions:
            if mode == "flat" and a.sub_actions:
                for sa in a.sub_actions:
                    items.append((sa, 0))
            elif mode == "compact":
                items.append((a, 0))
            else:  # "tree"
                items.append((a, 0))
                for sa in a.sub_actions:
                    items.append((sa, 1))
        return items

    def add_or_update(
        self,
        action_id: str,
        description: str = "",
        status: str = "RUNNING",
        duration_seconds: float | None = None,
        error: str | None = None,
        tool_name: str | None = None,
        target: str | None = None,
        code: str | None = None,
        diff: str | None = None,
        output: str | None = None,
        parent_id: str | None = None,
        role: str | None = None,
    ) -> TurnAction:
        """Add new action or update existing invocation in-place (§21)."""
        # 1. Update if already exists in top-level or sub_actions
        for a in self.all_actions:
            if a.action_id == action_id:
                a.status = status
                if description and (not a.description or a.description == a.action_id):
                    a.description = description
                if duration_seconds is not None:
                    a.duration_seconds = duration_seconds
                if error is not None:
                    a.error = error
                if tool_name is not None:
                    a.tool_name = tool_name
                if target is not None:
                    a.target = target
                if code is not None:
                    a.code = code
                if diff is not None:
                    a.diff = diff
                if output is not None:
                    a.output = output
                if role is not None:
                    a.role = role
                return a

        # 2. Check if this is a sub-action under a parent
        target_parent = None
        if parent_id:
            for a in self.actions:
                if a.action_id == parent_id:
                    target_parent = a
                    break
        elif tool_name and "harness" not in tool_name.lower():
            for a in reversed(self.actions):
                if a.status.upper() in {"RUNNING", "ACTIVE"} and (
                    "harness" in (a.tool_name or "").lower() or "harness" in a.description.lower()
                ):
                    target_parent = a
                    break

        new_act = TurnAction(
            action_id=action_id,
            description=description or action_id,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
            tool_name=tool_name,
            target=target,
            code=code,
            diff=diff,
            output=output,
            parent_id=target_parent.action_id if target_parent else parent_id,
            role=role,
        )
        if target_parent is not None:
            target_parent.sub_actions.append(new_act)
        else:
            self.actions.append(new_act)
        return new_act

    def complete_action(
        self,
        action_id: str,
        status: str = "COMPLETED",
        duration_seconds: float | None = None,
        error: str | None = None,
        tool_name: str | None = None,
        output: str | None = None,
        diff: str | None = None,
        code: str | None = None,
        role: str | None = None,
        target: str | None = None,
    ) -> TurnAction | None:
        if status.upper() in {"FAILED", "FAILURE", "ERROR", "BLOCKED"}:
            self.expanded = True
        matched = None
        # 1. Match exact action_id if provided
        if action_id:
            for a in self.all_actions:
                if a.action_id == action_id:
                    matched = a
                    break

        # 2. Match by tool_name in description or action_id (checking running sub-actions first)
        if not matched and tool_name:
            norm_tool = tool_name.lower().replace("_", " ")
            for a in reversed(self.actions):
                for sa in reversed(a.sub_actions):
                    if sa.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                        desc_norm = sa.description.lower().replace("_", " ")
                        if norm_tool in desc_norm or tool_name.lower() in sa.action_id.lower():
                            matched = sa
                            break
                if matched:
                    break
                if a.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                    desc_norm = a.description.lower().replace("_", " ")
                    if norm_tool in desc_norm or tool_name.lower() in a.action_id.lower():
                        matched = a
                        break

        # 3. Fallback: match most recent in-flight action (sub-action first)
        if not matched:
            for a in reversed(self.actions):
                for sa in reversed(a.sub_actions):
                    if sa.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                        matched = sa
                        break
                if matched:
                    break
                if a.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                    matched = a
                    break

        if not matched:
            fallback_desc = tool_name or action_id or "Action"
            if tool_name and "harness" in tool_name.lower():
                from cli.tui.events import format_action_description
                fallback_desc = format_action_description(tool_name, target=target, role=role)
            matched = TurnAction(
                action_id=action_id or f"act-{len(self.actions)+1}",
                description=fallback_desc,
                status=status,
                duration_seconds=duration_seconds,
                error=error,
                tool_name=tool_name,
                target=target,
                role=role,
            )
            self.actions.append(matched)

        matched.status = status
        if role and not getattr(matched, "role", None):
            matched.role = role
        if target and not getattr(matched, "target", None):
            matched.target = target
        if tool_name and not getattr(matched, "tool_name", None):
            matched.tool_name = tool_name
        if duration_seconds is not None:
            matched.duration_seconds = duration_seconds
        if error is not None:
            matched.error = error
        if output is not None:
            matched.output = output
        if diff is not None:
            matched.diff = diff
        if code is not None:
            matched.code = code
        return matched

    def complete_all(self, status: str = "COMPLETED") -> None:
        """Complete all in-flight actions and mark group inactive."""
        self.active = False
        for a in self.all_actions:
            if a.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                a.status = status

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
        return any(a.status.upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"} for a in self.all_actions)

    @property
    def count(self) -> int:
        vis = self.get_visible_actions()
        return len(vis) if vis else len(self.actions)

    @property
    def completed_count(self) -> int:
        vis = self.get_visible_actions()
        items = [item[0] for item in vis] if vis else self.actions
        return sum(1 for a in items if a.status.upper() in {"COMPLETED", "SUCCESS", "DONE", "PASSED"})

    @property
    def header_suffix(self) -> str:
        """Summary suffix describing completed and failed action counts."""
        failed = self.failed_count
        count = self.count
        if failed > 0:
            if self.active or self.has_running:
                return f"{count} ({failed} failed)"
            if self.completed_count > 0:
                return f"{self.completed_count} completed, {failed} failed"
            return f"{failed} failed"
        if self.active or self.has_running:
            return f"{count}"
        return f"{count} completed"

    def format_header_text(self) -> str:
        """Format the summary header line per §20 and §21."""
        arrow = "▾" if self.expanded else "▸"
        return f"{arrow} Actions · {self.header_suffix}"


@dataclass
class ChatMessage:
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: str = ""
    actions: ActionsGroup | None = None
    thinking: str | None = None
    turn_id: str | None = None
    thinking_expanded: bool = False
    is_command: bool = False


@dataclass
class ConversationScroll:
    """Manages independent vertical scroll position and activity tracking for the conversation column per §31."""

    scroll_offset: int = 0
    viewport_height: int | None = None
    has_new_activity: bool = False
    follow_bottom: bool = True
    max_offset: int | None = None
    total_lines: int | None = None

    def scroll_up(self, lines: int = 1) -> None:
        """Scroll view upward, detaching from live-following."""
        self.scroll_offset += max(1, lines)
        if self.max_offset is not None and self.max_offset > 0:
            self.scroll_offset = min(self.scroll_offset, self.max_offset)
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

    def set_scroll_offset(self, offset: int) -> None:
        """Set absolute scroll offset directly (e.g. from scrollbar drag or positioning)."""
        max_limit = self.max_offset if (self.max_offset is not None and self.max_offset > 0) else None
        if max_limit is not None:
            self.scroll_offset = max(0, min(offset, max_limit))
        else:
            self.scroll_offset = max(0, offset)
        if self.scroll_offset == 0:
            self.follow_bottom = True
            self.has_new_activity = False
        else:
            self.follow_bottom = False

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
        client_timeout: float = 0.0,
        context_window_tokens: int = 32_768,
        compaction_threshold: float = 0.85,
        compaction_keep_recent: int = 6,
    ) -> None:
        self.project_name = project_name
        self.session_id = session_id
        self.phase: ProjectPhase = ProjectPhase.INITIALIZING
        self.mouse_reporting_enabled: bool = True
        self.error: str | None = None
        self.plan: dict[str, Any] = {}
        self.plan_revisions: list[PlanRevision] = []
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
        if roles is not None:
            self.roles = dict(roles)
        else:
            self.roles = {
                "Architecture": RoleStatus("Architecture", backend="Claude", work_order_id="WO-001" if self.work_orders else None, state="RUNNING" if self.work_orders else "WAITING"),
                "Backend": RoleStatus("Backend", backend="Codex", work_order_id="WO-002" if self.work_orders else None, state="WAITING"),
                "Frontend": RoleStatus("Frontend", backend="AGY", work_order_id="WO-003" if self.work_orders else None, state="WAITING"),
                "Q/A": RoleStatus("Q/A", backend="Ollama", work_order_id="WO-004" if self.work_orders else None, state="WAITING"),
                "GitOps": RoleStatus("GitOps", backend="Ollama", work_order_id="WO-005" if self.work_orders else None, state="WAITING"),
            }
        if operations is not None:
            self.operations = dict(operations)
        elif roles is not None or not self.work_orders:
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
        self.composer_alert: str | None = None
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
        self.seen_harness_reports: set[str] = set()
        self.connection_status: str = "online"  # "online", "reconnecting", "offline"
        self.govern_verbosity: str = "tree"  # "tree" | "flat" | "compact"
        self.current_turn_actions: ActionsGroup = ActionsGroup(active=False, verbosity=self.govern_verbosity)
        self.current_turn_thinking: str = ""
        self.thinking_expanded: bool = True
        self.live_thinking_expanded: bool = True
        self.actions_expanded: bool = False
        self.active_tool_name: str | None = None
        self.active_tool_target: str | None = None
        self.active_tool_code: str | None = None
        self.last_tool_name: str | None = None
        self.last_tool_target: str | None = None
        self.last_tool_code: str | None = None
        self.last_tool_status: str | None = None
        self.client_timeout: float = client_timeout
        self.context_window_tokens = max(1, context_window_tokens)
        self.compaction_threshold = min(max(compaction_threshold, 0.1), 1.0)
        self.compaction_keep_recent = max(1, compaction_keep_recent)
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.cumulative_turn_tokens: int = 0
        self._legacy_prompt_tokens: int = 0
        self._legacy_completion_tokens: int = 0
        self._legacy_cumulative_turn_tokens: int = 0
        self._unattached_prompt_tokens: int = 0
        self._unattached_completion_tokens: int = 0
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.operation_tokens: dict[str, dict[str, int]] = {}
        self.compaction_count: int = 0
        self.settings_panel_open: bool = False
        self.settings_selected_row: int = 0
        self.settings_current_view: str = "main"  # "main", "provider:<name>", "roles"
        self.settings_field_idx: int = 0
        self.settings_status_msg: str = ""
        self.settings_obj: Any | None = None
        self.execution_profile: str = "medium"
        self.cycle_telemetry: list[dict[str, Any]] = []
        self.last_telemetry: dict[str, Any] | None = None
        self.attempt_count: int = 0
        self.max_attempts: int = 10
        self.retry_count: int = 0
        self.max_retries: int = 10
        self.retry_status: str | None = None
        self.active_working_status: str | None = None
        self.command_dialog_open: bool = False
        self.command_dialog_command: str = ""
        self.command_dialog_output: str = ""
        self.command_dialog_scroll: int = 0
        self.command_dialog_title: str = ""
        self._last_command_str: str | None = None
        self.runtime_panel_visible: bool = True

    def toggle_runtime_panel(self) -> bool:
        """Toggle visibility of the persistent runtime sidebar panel (Ctrl+B)."""
        self.runtime_panel_visible = not self.runtime_panel_visible
        return self.runtime_panel_visible

    def get_latest_suggested_goal(self) -> str | None:
        """Extract the most recently proposed :goal command from assistant messages."""

        for msg in reversed(self.messages):
            role = getattr(msg, "role", getattr(msg, "sender", ""))
            if role == "assistant":
                content = getattr(msg, "content", "")
                if not content:
                    continue
                # Match :goal followed by description (handles backticks, leading symbols, or newlines)
                m = re.search(r"(?:^|[^\w/]):goal\s+([^\n`\r]+)", content, re.MULTILINE)
                if m:
                    goal_text = m.group(1).strip()
                    goal_text = goal_text.rstrip("`'\"").lstrip("`'\"").strip()
                    if goal_text:
                        return goal_text
        return None

    @property
    def retry_status_display(self) -> str | None:
        """Formatted retry/attempt status for top-right border display (§retry)."""
        if self.retry_status is not None:
            return self.retry_status
        if self.attempt_count > 0 or self.retry_count > 0:
            return f"({self.attempt_count}/{self.max_attempts} Attempt . {self.retry_count}/{self.max_retries} Retry)"
        return None

    def record_attempt(
        self,
        attempt: int | None = None,
        retry: int | None = None,
        max_attempts: int | None = None,
        max_retries: int | None = None,
        status: str | None = None,
    ) -> str:
        """Record an attempt/retry update according to two-tier retry mechanism."""
        if max_attempts is not None:
            try:
                self.max_attempts = max(1, int(max_attempts))
            except (ValueError, TypeError):
                pass
        if max_retries is not None:
            try:
                self.max_retries = max(1, int(max_retries))
            except (ValueError, TypeError):
                pass

        if status is not None:
            self.retry_status = str(status)
            return self.retry_status

        if attempt is not None:
            try:
                self.attempt_count = int(attempt)
            except (ValueError, TypeError):
                self.attempt_count += 1
        else:
            if self.attempt_count >= self.max_attempts:
                self.attempt_count = 1
            else:
                self.attempt_count += 1

        if retry is not None:
            try:
                self.retry_count = int(retry)
            except (ValueError, TypeError):
                pass
        else:
            # When attempts are exhausted, count as 1/10 retry
            if self.attempt_count == self.max_attempts:
                self.retry_count += 1

        return self.retry_status_display or ""

    def reset_attempts_and_retries(self) -> None:
        """Reset attempt and retry counters for a fresh session or completed task."""
        self.attempt_count = 0
        self.retry_count = 0
        self.retry_status = None

    def open_settings(self, settings_obj: Any | None = None, initial_view: str = "main") -> None:
        """Open the interactive settings panel anchored below the composer."""
        self.settings_panel_open = True
        self.settings_selected_row = 0
        self.settings_current_view = initial_view
        self.settings_status_msg = ""
        if settings_obj is not None:
            self.settings_obj = settings_obj

    def close_settings(self) -> None:
        """Close the interactive settings panel and restore normal viewport."""
        self.settings_panel_open = False
        self.settings_selected_row = 0
        self.settings_current_view = "main"
        self.settings_status_msg = ""

    def show_command_dialog(self, command: str, output: str, title: str | None = None) -> None:
        """Open system command dialog just above the composer text area."""
        self.command_dialog_open = True
        self.command_dialog_command = command
        self.command_dialog_output = output
        self.command_dialog_scroll = 0
        self.command_dialog_title = title or ""

    def close_command_dialog(self) -> None:
        """Close system command dialog and restore normal conversation viewport."""
        self.command_dialog_open = False
        self.command_dialog_command = ""
        self.command_dialog_output = ""
        self.command_dialog_scroll = 0
        self.command_dialog_title = ""

    def scroll_command_dialog_up(self, lines: int = 1) -> None:
        """Scroll command dialog content upward."""
        if self.command_dialog_scroll > 0:
            self.command_dialog_scroll = max(0, self.command_dialog_scroll - lines)

    def scroll_command_dialog_down(self, lines: int = 1, max_lines: int = 1000) -> None:
        """Scroll command dialog content downward."""
        self.command_dialog_scroll = min(max_lines, self.command_dialog_scroll + lines)

    def record_command_output(self, command: str, output: str, title: str | None = None) -> None:
        """Record command execution and display output in dialog above text area without cluttering chat."""
        self.add_message("user", command, is_command=True)
        msg = self.add_message("system", output, is_command=True)
        try:
            setattr(msg, "_is_command_output", True)
        except Exception:
            pass
        self.show_command_dialog(command, output, title=title)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Return a fast, dependency-free token estimate suitable for local models."""
        return max(0, math.ceil(len(text or "") / 4))

    def _recompute_token_totals(self) -> None:
        op_p = sum(op.get("prompt", 0) for op in self.operation_tokens.values())
        op_c = sum(op.get("completion", 0) for op in self.operation_tokens.values())
        total_p = op_p + self._unattached_prompt_tokens
        total_c = op_c + self._unattached_completion_tokens
        self.prompt_tokens = max(total_p, self._legacy_prompt_tokens)
        self.completion_tokens = max(total_c, self._legacy_completion_tokens)
        self.cumulative_turn_tokens = max(total_p + total_c, self._legacy_cumulative_turn_tokens)

    def record_operation_tokens(
        self,
        op_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Record provider-measured tokens for an operation to avoid double-counting across retries."""
        if prompt_tokens > 0:
            self.last_prompt_tokens = prompt_tokens
        if completion_tokens > 0:
            self.last_completion_tokens = completion_tokens
        if op_id:
            curr = self.operation_tokens.get(op_id, {"prompt": 0, "completion": 0, "total": 0})
            new_p = max(curr["prompt"], prompt_tokens)
            new_c = max(curr["completion"], completion_tokens)
            self.operation_tokens[op_id] = {
                "prompt": new_p,
                "completion": new_c,
                "total": new_p + new_c,
            }
        else:
            self._unattached_prompt_tokens = max(self._unattached_prompt_tokens, prompt_tokens)
            self._unattached_completion_tokens = max(self._unattached_completion_tokens, completion_tokens)
        self._recompute_token_totals()

    def record_token_usage(
        self,
        prompt: str = "",
        completion: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Accumulate per-session prompt/completion estimates for observability."""
        p_count = prompt_tokens if prompt_tokens is not None else self.estimate_tokens(prompt)
        c_count = completion_tokens if completion_tokens is not None else self.estimate_tokens(completion)
        self._legacy_prompt_tokens += p_count
        self._legacy_completion_tokens += c_count
        self._legacy_cumulative_turn_tokens += p_count + c_count
        self._recompute_token_totals()

    def estimate_transcript_tokens(self) -> int:
        """Return estimated tokens across all retained messages, thinking, and actions."""
        total = 0
        for message in self.messages:
            total += self.estimate_tokens(message.content)
            if message.thinking:
                total += self.estimate_tokens(message.thinking)
            if message.actions:
                for a in message.actions.all_actions:
                    if a.description:
                        total += self.estimate_tokens(a.description)
                    if a.target:
                        total += self.estimate_tokens(a.target)
                    if a.diff:
                        total += self.estimate_tokens(a.diff)
                    if a.code:
                        total += self.estimate_tokens(a.code)
                    if a.output:
                        total += self.estimate_tokens(a.output)
                    if a.error:
                        total += self.estimate_tokens(a.error)
        if getattr(self, "current_turn_thinking", None):
            total += self.estimate_tokens(self.current_turn_thinking)
        turn_actions = getattr(self, "current_turn_actions", None)
        if turn_actions and hasattr(turn_actions, "all_actions"):
            for a in turn_actions.all_actions:
                if a.description:
                    total += self.estimate_tokens(a.description)
                if a.target:
                    total += self.estimate_tokens(a.target)
                if a.diff:
                    total += self.estimate_tokens(a.diff)
                if a.code:
                    total += self.estimate_tokens(a.code)
                if a.output:
                    total += self.estimate_tokens(a.output)
                if a.error:
                    total += self.estimate_tokens(a.error)
        return total

    @property
    def context_tokens(self) -> int:
        """Estimated tokens currently retained in or evaluated by the active model context."""
        transcript_est = self.estimate_transcript_tokens()
        active_prompt = getattr(self, "last_prompt_tokens", 0) or 0
        active_in_progress = self.estimate_tokens(getattr(self, "current_turn_thinking", ""))
        if hasattr(self, "last_completion_tokens") and self.last_completion_tokens:
            active_in_progress = max(active_in_progress, self.last_completion_tokens)
        model_context = active_prompt + active_in_progress if active_prompt > 0 else 0
        total = max(transcript_est, model_context)
        # Dynamic self-healing: if measured tokens exceed context_window_tokens,
        # expand to the next standard context tier so the meter reflects reality
        # rather than being pegged at an outdated default.
        if total > self.context_window_tokens:
            for tier in (16_384, 32_768, 65_536, 131_072, 262_144):
                if tier > total:
                    self.context_window_tokens = tier
                    break
            else:
                self.context_window_tokens = int(total * 1.5)
        return total

    @property
    def context_utilization(self) -> float:
        return min(1.0, self.context_tokens / self.context_window_tokens)

    @property
    def context_warning_level(self) -> str:
        utilization = self.context_utilization
        if utilization >= self.compaction_threshold:
            return "red"
        if utilization >= 0.70:
            return "yellow"
        return "green"

    @property
    def context_meter_text(self) -> str:
        return f"Context: {self.context_tokens / 1000:.1f}K / {self.context_window_tokens / 1000:.1f}K ({self.context_utilization:.0%})"

    def set_context_window(self, tokens: int) -> None:
        """Set a detected model context capacity, retaining a safe positive value."""
        self.context_window_tokens = max(1, int(tokens))

    def compact_transcript(self, keep_recent: int | None = None) -> bool:
        """Replace older transcript turns with a concise local continuity summary."""
        keep = self.compaction_keep_recent if keep_recent is None else max(1, keep_recent)
        if len(self.messages) <= keep:
            return False

        removed = self.messages[:-keep]
        retained = self.messages[-keep:]
        milestones = [
            f"{message.role}: {' '.join(message.content.split())[:120]}"
            for message in removed
            if message.content.strip()
        ]
        excerpt = " | ".join(milestones[-3:])
        summary = (
            f"[Transcript compacted: {len(removed)} earlier messages preserved as context. "
            f"Recent milestones: {excerpt}]"
        )
        summary_message = ChatMessage(role="system", content=summary, timestamp=self.now_str())
        self.messages = [summary_message, *retained]
        # Preserve all historical seen_message_keys so compacted turns are never re-added as duplicates
        self.seen_message_keys.add((summary_message.role, summary_message.content.strip()))
        self.compaction_count += 1
        self.conversation_scroll.notify_activity()
        self.conversation_scroll.max_offset = None
        return True

    def maybe_compact_transcript(self) -> bool:
        """Automatically compact before the transcript exhausts its context budget."""
        if self.context_utilization >= self.compaction_threshold:
            return self.compact_transcript()
        return False

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
        is_command: bool = False,
    ) -> ChatMessage:
        clean = content.strip()
        if not is_command and role == "user" and clean.startswith(":"):
            is_command = True
            self._last_command_str = clean
        elif role == "user":
            self._last_command_str = None
            self.close_command_dialog()
        elif role == "system" and getattr(self, "_last_command_str", None):
            is_command = True
            cmd_name = self._last_command_str
            self._last_command_str = None
            self.show_command_dialog(cmd_name, content)
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
            if msg_thinking is None:
                if getattr(self, "current_turn_thinking", None):
                    msg_thinking = self.current_turn_thinking
                elif "<think" in content.lower():
                    msg_thinking = extract_internal_reasoning(content)
            self.current_turn_thinking = ""
            if "<think" in msg_content.lower():
                msg_content = strip_internal_reasoning(msg_content)

        msg_actions = actions
        if role == "assistant":
            from cli.tui.chat import format_contract_payload
            msg_content = format_contract_payload(msg_content)
            for m in self.messages:
                if m.role == "assistant" and getattr(m, "actions", None):
                    m.actions.expanded = False
            if msg_actions is None and self.current_turn_actions and not self.current_turn_actions.is_empty:
                msg_actions = self.current_turn_actions
                msg_actions.complete_all()
                last_c = getattr(self, "last_tool_code", None) or getattr(self, "active_tool_code", None)
                last_t = getattr(self, "last_tool_target", None) or getattr(self, "active_tool_target", None)
                last_n = getattr(self, "last_tool_name", None) or getattr(self, "active_tool_name", None)
                if last_c and last_n in {"write_file", "write", "edit_file", "edit", "create_file", "create"}:
                    msg_actions.preview_label = f"✓ Wrote `{last_t}`" if last_t else "✓ Wrote code"
                    msg_actions.preview_code = last_c
                msg_actions.expanded = False
            elif msg_actions is not None:
                msg_actions.complete_all()
            self.active_tool_name = None
            self.active_tool_target = None
            self.active_tool_code = None
            self.last_tool_name = None
            self.last_tool_target = None
            self.last_tool_code = None
            if msg_actions is self.current_turn_actions:
                self.current_turn_actions = ActionsGroup(active=False, verbosity=self.govern_verbosity)

        if msg_actions and getattr(self, "actions_expanded", False):
            msg_actions.expanded = self.actions_expanded

        msg = ChatMessage(
            role=role,
            content=msg_content,
            timestamp=self.now_str(),
            actions=msg_actions,
            thinking=msg_thinking,
            turn_id=turn_id,
            thinking_expanded=self.thinking_expanded,
            is_command=is_command,
        )
        self.messages.append(msg)

        if role == "user":
            self.record_token_usage(prompt=msg_content)
        elif role == "assistant":
            self.record_token_usage(completion=msg_content)
        if hasattr(self, "conversation_scroll") and self.conversation_scroll is not None:
            self.conversation_scroll.notify_activity()
            self.conversation_scroll.max_offset = None
            self.conversation_scroll.total_lines = None
        self.maybe_compact_transcript()
        return msg

    def toggle_thinking(self, index: int | None = None) -> bool:
        """Toggle thinking block expansion globally or for a specific message."""
        if index is not None and 0 <= index < len(self.messages):
            self.messages[index].thinking_expanded = not getattr(self.messages[index], "thinking_expanded", False)
            return self.messages[index].thinking_expanded
        self.thinking_expanded = not self.thinking_expanded
        self.live_thinking_expanded = self.thinking_expanded
        for m in self.messages:
            if getattr(m, "thinking", None):
                m.thinking_expanded = self.thinking_expanded
        return self.thinking_expanded

    def expand_thinking(self, index: int | None = None) -> None:
        """Expand thinking blocks globally or for a specific message."""
        if index is not None and 0 <= index < len(self.messages):
            self.messages[index].thinking_expanded = True
            return
        self.thinking_expanded = True
        self.live_thinking_expanded = True
        for m in self.messages:
            if getattr(m, "thinking", None):
                m.thinking_expanded = True

    def collapse_thinking(self, index: int | None = None) -> None:
        """Collapse thinking blocks globally or for a specific message."""
        if index is not None and 0 <= index < len(self.messages):
            self.messages[index].thinking_expanded = False
            return
        self.thinking_expanded = False
        self.live_thinking_expanded = False
        for m in self.messages:
            if getattr(m, "thinking", None):
                m.thinking_expanded = False

    def scroll_conversation_up(self, lines: int = 1) -> None:
        """Scroll conversation viewport upward, detaching from live-follow (§31)."""
        self.conversation_scroll.scroll_up(lines)

    def scroll_conversation_down(self, lines: int = 1) -> None:
        """Scroll conversation viewport downward towards bottom (§31)."""
        self.conversation_scroll.scroll_down(lines)

    def scroll_conversation_to_bottom(self) -> None:
        """Jump conversation viewport to bottom and resume live-following (§31)."""
        self.conversation_scroll.scroll_to_bottom()

    def set_conversation_scroll_offset(self, offset: int) -> None:
        """Set absolute conversation viewport scroll offset directly."""
        self.conversation_scroll.set_scroll_offset(offset)

    def record_turn_action(
        self,
        action_id: str,
        description: str = "",
        status: str = "RUNNING",
        duration_seconds: float | None = None,
        error: str | None = None,
        tool_name: str | None = None,
        target: str | None = None,
        code: str | None = None,
        diff: str | None = None,
        output: str | None = None,
        parent_id: str | None = None,
    ) -> TurnAction:
        """Record or update a turn action in-place (§21)."""
        if not self.current_turn_actions.active:
            self.current_turn_actions.active = True
        self.current_turn_actions.verbosity = self.govern_verbosity
        action = self.current_turn_actions.add_or_update(
            action_id=action_id,
            description=description,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
            tool_name=tool_name,
            target=target,
            code=code,
            diff=diff,
            output=output,
            parent_id=parent_id,
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
        tool_name: str | None = None,
        output: str | None = None,
        diff: str | None = None,
        code: str | None = None,
    ) -> TurnAction | None:
        """Complete a turn action in-place (§21)."""
        act = self.current_turn_actions.complete_action(
            action_id=action_id,
            status=status,
            duration_seconds=duration_seconds,
            error=error,
            tool_name=tool_name,
            output=output,
            diff=diff,
            code=code,
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
        latest = self.get_latest_actions_group()
        new_expanded = not (latest.expanded if latest else getattr(self, "actions_expanded", False))
        self.actions_expanded = new_expanded
        if self.current_turn_actions:
            self.current_turn_actions.expanded = new_expanded
        for m in self.messages:
            if m.actions:
                m.actions.expanded = new_expanded
        return new_expanded

    def expand_actions(self, turn_index: int | None = None) -> None:
        """Expand actions disclosure via keyboard (§22)."""
        if turn_index is not None:
            asst_msgs = [m for m in self.messages if m.role == "assistant" and m.actions]
            if 0 <= turn_index < len(asst_msgs):
                asst_msgs[turn_index].actions.expand()  # type: ignore[union-attr]
                return
        self.actions_expanded = True
        if self.current_turn_actions:
            self.current_turn_actions.expand()
        for m in self.messages:
            if m.actions:
                m.actions.expand()

    def collapse_actions(self, turn_index: int | None = None) -> None:
        """Collapse actions disclosure via keyboard (§22)."""
        if turn_index is not None:
            asst_msgs = [m for m in self.messages if m.role == "assistant" and m.actions]
            if 0 <= turn_index < len(asst_msgs):
                asst_msgs[turn_index].actions.collapse()  # type: ignore[union-attr]
                return
        self.actions_expanded = False
        if self.current_turn_actions:
            self.current_turn_actions.collapse()
        for m in self.messages:
            if m.actions:
                m.actions.collapse()

    def toggle_disclosures(self) -> bool:
        """Toggle all collapsible disclosures (thinking + actions) harmoniously (§22)."""
        has_actions = any(m.actions and not m.actions.is_empty for m in self.messages) or (
            self.current_turn_actions and not self.current_turn_actions.is_empty
        )
        has_thinking = any(getattr(m, "thinking", None) for m in self.messages) or bool(self.current_turn_thinking)

        actions_collapsed = False
        if has_actions:
            latest_act = self.get_latest_actions_group()
            if latest_act and not latest_act.expanded:
                actions_collapsed = True
            elif not getattr(self, "actions_expanded", False):
                actions_collapsed = True

        thinking_collapsed = False
        if has_thinking:
            if not getattr(self, "thinking_expanded", True):
                thinking_collapsed = True
            elif any(getattr(m, "thinking", None) and not getattr(m, "thinking_expanded", False) for m in self.messages):
                thinking_collapsed = True

        if has_actions and not has_thinking:
            return self.toggle_actions()
        elif has_thinking and not has_actions:
            return self.toggle_thinking()
        elif has_actions and has_thinking:
            target_expand = actions_collapsed or thinking_collapsed
            if target_expand:
                self.expand_thinking()
                self.expand_actions()
                return True
            else:
                self.collapse_thinking()
                self.collapse_actions()
                return False
        else:
            self.toggle_thinking()
            return self.toggle_actions()

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
        self.seen_message_keys.clear()
        self.current_turn_actions = ActionsGroup(active=False)
        if hasattr(self, "conversation_scroll") and self.conversation_scroll is not None:
            self.conversation_scroll.scroll_to_bottom()
            self.conversation_scroll.max_offset = None

    def update_from_session(self, session: Mapping[str, Any]) -> None:
        if not session or not isinstance(session, Mapping):
            return
        self.session_id = str(session.get("session_id", self.session_id))
        if "execution_profile" in session and session["execution_profile"]:
            self.execution_profile = str(session["execution_profile"])
        for key in ("context_window_tokens", "context_window", "context_length", "num_ctx"):
            value = session.get(key)
            if isinstance(value, (int, float)) and value > 0:
                self.set_context_window(int(value))
                break
        st_raw = str(session.get("state", "")).upper()
        if st_raw == "CANCELLED":
            for op in self.operations.values():
                if op.status.upper() in {"RUNNING", "ACTIVE"}:
                    op.status = "CANCELLED"
            for role_obj in self.roles.values():
                if role_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                    role_obj.state = "CANCELLED"
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
                if op_id:
                    status = entry.get("status", "RUNNING")
                    if op_id not in self.operations:
                        role = entry.get("role") or self._infer_role_from_op(entry.get("operation", ""))
                        backend = entry.get("backend") or "Codex"
                        parent_id = entry.get("parent_operation_id")
                        node = OperationNode(op_id, entry.get("operation", role), role=role, backend=backend, status=status, parent_id=parent_id)
                        self.operations[op_id] = node
                        if parent_id and parent_id in self.operations:
                            if op_id not in self.operations[parent_id].children:
                                self.operations[parent_id].children.append(op_id)
                    else:
                        self.operations[op_id].status = status
        active_op = session.get("active_operation")
        if not active_op and "op-root" in self.operations:
            self.operations.pop("op-root", None)

        run_dict = session.get("run") if isinstance(session.get("run"), Mapping) else {}
        run_phase = session.get("phase") or run_dict.get("phase")
        run_error = session.get("error") or run_dict.get("error")
        if run_error:
            self.error = str(run_error)

        if run_phase:
            rp_str = str(run_phase).upper()
            if rp_str in ("DISPATCHING", "EXECUTING", "AUTHORING", "INTEGRATION_REVIEW",
                          "AUTHORING_READINESS_GATE", "ARCHITECT_REPAIR", "ARCHITECT_RECOVERY_DECISION",
                          "PRODUCT_READY", "GITOPS"):
                self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                if not run_error:
                    self.error = None
            elif rp_str == "AWAITING_APPROVAL":
                self.phase = ProjectPhase.AWAITING_APPROVAL
            elif rp_str in ("COMPLETE",):
                self.phase = ProjectPhase.PROJECT_COMPLETE
                self.is_complete = True
                self.error = None
                for k in self.completion_checklist:
                    self.completion_checklist[k] = True
                for r_obj in self.roles.values():
                    if r_obj.state.upper() in {"RUNNING", "ACTIVE", "WAITING"}:
                        r_obj.state = "COMPLETED"
                for op in self.operations.values():
                    if op.status.upper() in {"RUNNING", "ACTIVE"}:
                        op.status = "COMPLETED"
            elif rp_str == "BLOCKED":
                self.phase = ProjectPhase.BLOCKED
            elif rp_str == "FAILED":
                self.phase = ProjectPhase.FAILED

        ws_session = session.get("workspace")
        if ws_session:
            self.sync_work_orders_from_disk(ws_session)
        journal = session.get("journal")
        if isinstance(journal, list):
            for rec in journal:
                if isinstance(rec, Mapping):
                    op_id = rec.get("operation_id")
                    result = rec.get("result")
                    if isinstance(result, Mapping):
                        telem = result.get("telemetry")
                        if isinstance(telem, Mapping):
                            p_tok = int(telem.get("prompt_tokens", 0) or 0)
                            c_tok = int(telem.get("completion_tokens", 0) or 0)
                            self.record_operation_tokens(op_id, prompt_tokens=p_tok, completion_tokens=c_tok)
        self.reconcile_roles_from_work_orders()

    def sync_work_orders(self, wo_records: list[dict[str, Any]]) -> None:
        if not isinstance(wo_records, list):
            return
        items = []
        completed_ids = {
            str(r.get("id") or r.get("wo_id"))
            for r in wo_records
            if isinstance(r, Mapping) and str(r.get("status", "")).upper() == "COMPLETED"
        }
        running_op_wos = {
            getattr(op, "work_order_id", None) or op.name
            for op in self.operations.values()
            if op.status == "RUNNING"
        }
        first_runnable = None
        for r in wo_records:
            if not isinstance(r, Mapping):
                continue
            wo_id = str(r.get("id") or r.get("wo_id") or "WO-xxx")
            title = str(r.get("title", f"Work order {wo_id}"))
            agents = r.get("assigned_agents")
            role = agents[0].title() if isinstance(agents, list) and agents else "Backend"
            priority = str(r.get("priority", "P0"))
            raw_st = str(r.get("status", "WAITING")).upper()
            deps = r.get("dependencies") or []
            if raw_st in {"COMPLETED", "BLOCKED", "CANCELLED"}:
                status = raw_st
            else:
                has_unmet_deps = isinstance(deps, list) and any(d not in completed_ids for d in deps)
                if has_unmet_deps:
                    status = "WAITING"
                elif wo_id in running_op_wos:
                    status = "RUNNING"
                elif first_runnable is None:
                    first_runnable = wo_id
                    status = "RUNNING"
                else:
                    status = "WAITING"
            items.append(WorkOrderItem(wo_id, title, role=role, priority=priority, status=status))
        if items:
            self.work_orders = items
        self.reconcile_roles_from_work_orders()

    def sync_work_orders_from_disk(self, workspace: Path | str | None = None) -> None:
        """Synchronize work order statuses accurately from disk (.sync/work-orders/INDEX.yaml and folders)."""
        ws_val = workspace or getattr(self, "workspace", None)
        if not ws_val:
            return
        try:
            ws_path = Path(ws_val)
            if not ws_path.is_dir():
                return
            sync_wos = ws_path / ".sync" / "work-orders"
            if not sync_wos.is_dir():
                return

            import yaml
            disk_statuses: dict[str, str] = {}
            disk_orders: dict[str, dict[str, Any]] = {}

            completed_dir = sync_wos / "COMPLETED"
            if completed_dir.is_dir():
                for f in completed_dir.glob("*.yaml"):
                    disk_statuses[f.stem] = "COMPLETED"
                    try:
                        data = yaml.safe_load(f.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            disk_orders[f.stem] = data
                    except Exception:
                        pass

            blocked_dir = sync_wos / "BLOCKED"
            if blocked_dir.is_dir():
                for f in blocked_dir.glob("*.yaml"):
                    disk_statuses[f.stem] = "BLOCKED"
                    try:
                        data = yaml.safe_load(f.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            disk_orders[f.stem] = data
                    except Exception:
                        pass

            active_dir = sync_wos / "ACTIVE"
            if active_dir.is_dir():
                for f in active_dir.glob("*.yaml"):
                    try:
                        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                        st = str(data.get("status", "ACTIVE")).upper()
                        if data.get("error") or data.get("blocked_reason"):
                            st = "BLOCKED"
                        disk_statuses[f.stem] = st
                        disk_orders[f.stem] = data
                    except Exception:
                        disk_statuses.setdefault(f.stem, "ACTIVE")

            index_file = sync_wos / "INDEX.yaml"
            if index_file.is_file():
                try:
                    idx = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
                    for o in idx.get("orders", []):
                        if isinstance(o, dict) and o.get("id"):
                            oid = str(o["id"])
                            ost = str(o.get("status", "")).upper()
                            if ost:
                                disk_statuses[oid] = ost
                            disk_orders[oid] = o
                except Exception:
                    pass

            default_titles = {
                "Architecture & Orchestration",
                "Backend API & Services",
                "Frontend Client & Views",
                "Q/A Verification Suite",
                "GitOps Release & Hygiene",
            }
            is_dummy = bool(self.work_orders) and all(w.title in default_titles for w in self.work_orders)

            if not disk_statuses and not disk_orders:
                if is_dummy:
                    self.work_orders = []
                    for r in self.roles.values():
                        if r.work_order_id in {"WO-001", "WO-002", "WO-003", "WO-004", "WO-005"}:
                            r.work_order_id = None
                self.reconcile_roles_from_work_orders()
                return

            # Compute effective statuses using dependencies and running operations
            completed_ids = {oid for oid, st in disk_statuses.items() if st == "COMPLETED"}
            running_op_wos = {
                getattr(op, "work_order_id", None) or op.name
                for op in self.operations.values()
                if op.status == "RUNNING"
            }
            active_candidates = [
                oid for oid in sorted(disk_orders.keys())
                if disk_statuses.get(oid) not in {"COMPLETED", "BLOCKED", "CANCELLED"}
            ]

            first_runnable = None
            for oid in active_candidates:
                o = disk_orders[oid]
                deps = o.get("dependencies") or []
                has_unmet_deps = isinstance(deps, list) and any(
                    dep in disk_orders and dep not in completed_ids for dep in deps
                )
                if has_unmet_deps:
                    disk_statuses[oid] = "WAITING"
                elif oid in running_op_wos:
                    disk_statuses[oid] = "RUNNING"
                elif first_runnable is None:
                    first_runnable = oid
                    disk_statuses[oid] = "RUNNING"
                else:
                    disk_statuses[oid] = "WAITING"

            default_titles = {
                "Architecture & Orchestration",
                "Backend API & Services",
                "Frontend Client & Views",
                "Q/A Verification Suite",
                "GitOps Release & Hygiene",
            }
            is_dummy = self.work_orders and all(w.title in default_titles for w in self.work_orders)

            if disk_orders and (not self.work_orders or is_dummy or set(disk_orders.keys()) != {w.id for w in self.work_orders}):
                items = []
                for oid, o in sorted(disk_orders.items()):
                    title = str(o.get("title", f"Work order {oid}"))
                    agents = o.get("assigned_agents")
                    role = agents[0].title() if isinstance(agents, list) and agents else "Backend"
                    priority = str(o.get("priority", "P0"))
                    st = disk_statuses.get(oid, str(o.get("status", "WAITING")).upper())
                    items.append(WorkOrderItem(oid, title, role=role, priority=priority, status=st))
                self.work_orders = items
            else:
                for w in self.work_orders:
                    if w.id in disk_statuses:
                        w.status = disk_statuses[w.id]
        except Exception:
            pass
        self.reconcile_roles_from_work_orders()

    def reconcile_roles_from_work_orders(self) -> None:
        """Reconcile role statuses against completed work orders and operations."""
        if not self.roles:
            return

        if self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE:
            for r_obj in self.roles.values():
                if r_obj.state.upper() in {"RUNNING", "ACTIVE", "WAITING"}:
                    r_obj.state = "COMPLETED"
            for op in self.operations.values():
                if op.status.upper() in {"RUNNING", "ACTIVE"}:
                    op.status = "COMPLETED"
            return

        # Check if all work orders in the project are complete
        has_subsequent_review = any(
            w.id > "WO-014" and w.status.upper() in {"COMPLETED", "DONE", "SUCCESS"}
            for w in self.work_orders
        )
        effective_wos = [
            w for w in self.work_orders
            if not (w.id == "WO-014" and w.status.upper() == "BLOCKED" and has_subsequent_review)
        ]
        if effective_wos and all(w.status.upper() in {"COMPLETED", "DONE", "SUCCESS"} for w in effective_wos):
            for r_obj in self.roles.values():
                if r_obj.state.upper() in {"RUNNING", "ACTIVE", "WAITING"}:
                    r_obj.state = "COMPLETED"
            for op in self.operations.values():
                if op.status.upper() in {"RUNNING", "ACTIVE"}:
                    op.status = "COMPLETED"
            return

        # Per-role reconciliation: if all work orders for a role are done and no op running
        running_roles = {
            (op.role or "").lower()
            for op_id, op in self.operations.items()
            if str(op.status).upper() in {"RUNNING", "ACTIVE"}
            and op_id != "op-root"
        }

        # If no non-root operations are running, close synthetic op-root
        if not running_roles and "op-root" in self.operations:
            self.operations["op-root"].status = "COMPLETED"

        for r_name, r_obj in self.roles.items():
            if r_name.lower() in {"architecture", "lead"}:
                continue
            role_wos = [
                w for w in effective_wos
                if w.role.lower() == r_name.lower()
                or (r_name.lower() == "backend" and w.role.lower() in {"codex", "backend"})
                or (r_name.lower() == "frontend" and w.role.lower() in {"gemini", "frontend", "agy"})
                or (r_name.lower() in {"qa", "q/a"} and w.role.lower() in {"gemma", "qa", "q/a"})
                or (r_name.lower() == "gitops" and w.role.lower() in {"local-llm", "gitops"})
            ]
            if not self.work_orders and r_name.lower() not in running_roles:
                if r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                    r_obj.state = "WAITING"
            elif role_wos and all(w.status.upper() in {"COMPLETED", "DONE", "SUCCESS"} for w in role_wos):
                if r_name.lower() not in running_roles:
                    r_obj.state = "COMPLETED"
            elif role_wos and not any(w.status.upper() in {"RUNNING", "ACTIVE", "IN_PROGRESS"} for w in role_wos):
                if r_name.lower() not in running_roles and r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                    r_obj.state = "WAITING"

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
            seen_roles: set[str] = set()
            for r_name, r_obj in self.roles.items():
                r_norm = str(getattr(r_obj, "role", r_name) or r_name).strip()
                r_key = r_norm.lower()
                if r_key in seen_roles:
                    continue
                seen_roles.add(r_key)
                if r_key == "architecture" or "arch" in r_key or "lead" in r_key:
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
                has_subsequent_review = any(
                    w.id > "WO-014" and w.status.upper() in {"COMPLETED", "DONE", "SUCCESS"}
                    for w in self.work_orders
                )
                effective_wos = [
                    w for w in self.work_orders
                    if not (w.id == "WO-014" and w.status.upper() == "BLOCKED" and has_subsequent_review)
                ]
                if self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE:
                    r_stat = "completed"
                elif effective_wos and all(w.status.upper() in {"COMPLETED", "DONE", "SUCCESS"} for w in effective_wos):
                    r_stat = "completed"
                elif root_obj.state.upper() in {"COMPLETED", "DONE", "VERIFIED"} and (self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE):
                    r_stat = "completed"
                else:
                    r_stat = "orchestrating" if "arch" in root_obj.role.lower() else root_obj.display_state
                entries.append({
                    "name": root_obj.role,
                    "role": root_obj.role,
                    "backend": root_obj.backend,
                    "status": r_stat,
                    "is_root": True,
                })
            for w_obj in children_objs:
                w_status = w_obj.display_state
                if self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE:
                    if w_status in {"implementing", "running", "waiting", "orchestrating"}:
                        w_status = "completed"
                elif self.phase == ProjectPhase.BLOCKED:
                    if w_obj.role.lower() == "gitops" and (
                        "gitops" in str(getattr(self, "error", "")).lower()
                        or any(w.status == "BLOCKED" for w in self.work_orders if w.role.lower() in ("gitops", "local-llm"))
                    ):
                        w_status = "blocked"
                    elif w_status == "implementing":
                        w_status = "idle"
                entries.append({
                    "name": w_obj.role,
                    "role": w_obj.role,
                    "backend": w_obj.backend,
                    "status": w_status,
                    "is_root": False,
                })
            return entries

        # 2. Build from operations if no roles defined
        if self.operations:
            root = self.operations.get("op-root")
            children_ids = root.children if root else [k for k in self.operations if k != "op-root"]
            if root:
                if self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE or str(root.status).upper() in {"COMPLETED", "DONE", "VERIFIED"}:
                    r_status = "completed"
                else:
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
                    n_stat = "completed" if (self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE) else node.status.lower()
                    entries.append({
                        "name": node.name or node.role,
                        "role": node.role,
                        "backend": node.backend,
                        "status": n_stat,
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
                        is_running_arch = (
                            normalized.lower() == "architecture"
                            and (
                                any(str(op.status).upper() in {"RUNNING", "ACTIVE"} for op_id, op in self.operations.items() if op_id != "op-root")
                                or any(w.status == "RUNNING" for w in self.work_orders)
                            )
                        )
                        new_roles[normalized] = RoleStatus(
                            role=normalized,
                            backend=b_end,
                            model=m_del,
                            state="RUNNING" if is_running_arch else "WAITING",
                        )
                    if new_roles:
                        self.roles = new_roles
            except Exception:
                pass

            # Sync / enrich roles from settings on disk if available
            try:
                from validators.kernel.settings import find_settings_file, load_settings
                ws = workspace or getattr(self, "workspace", None)
                if find_settings_file(ws):
                    s = load_settings(ws)
                    if hasattr(s, "govern_verbosity") and s.govern_verbosity:
                        self.govern_verbosity = s.govern_verbosity
                        if hasattr(self, "current_turn_actions") and self.current_turn_actions:
                            self.current_turn_actions.verbosity = self.govern_verbosity
                    for r_name, r_cfg in s.roles.items():
                        r_title = "Q/A" if r_name.upper() in {"QA", "Q/A"} else r_name.title()
                        if r_title in self.roles:
                            if r_cfg.get("provider"):
                                self.roles[r_title].backend = r_cfg["provider"]
                            if r_cfg.get("model"):
                                self.roles[r_title].model = r_cfg["model"]
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

        if workspace is not None:
            try:
                ws_path = Path(workspace)
                self.sync_work_orders_from_disk(ws_path)

                plan_file = ws_path / ".sync" / "PLAN.md"
                if not plan_file.exists():
                    plan_file = ws_path / "PLAN.md"
                if plan_file.exists():
                    self.completion_checklist["PLAN.md"] = True
                    if self.phase in (ProjectPhase.INITIALIZING, ProjectPhase.AWAITING_APPROVAL):
                        active_wos_dir = ws_path / ".sync" / "work-orders" / "ACTIVE"
                        if active_wos_dir.is_dir() and any(active_wos_dir.glob("*.yaml")):
                            self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
            except Exception:
                pass

        default_titles = {
            "Architecture & Orchestration",
            "Backend API & Services",
            "Frontend Client & Views",
            "Q/A Verification Suite",
            "GitOps Release & Hygiene",
        }
        if self.work_orders and all(w.title in default_titles for w in self.work_orders):
            self.work_orders = []
            for r in self.roles.values():
                if r.work_order_id in {"WO-001", "WO-002", "WO-003", "WO-004", "WO-005"}:
                    r.work_order_id = None
        self.reconcile_roles_from_work_orders()

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

        # Two-tier attempt and retry tracking (§retry)
        if any(k in payload for k in ("attempt", "retry", "max_attempts", "max_retries", "retry_status")):
            self.record_attempt(
                attempt=payload.get("attempt"),
                retry=payload.get("retry"),
                max_attempts=payload.get("max_attempts"),
                max_retries=payload.get("max_retries"),
                status=payload.get("retry_status"),
            )
        elif payload.get("prompt"):
            prompt_text = str(payload.get("prompt"))
            import re
            m = re.search(r"\((\d+)/(\d+)\s+Attempt\s+\.\s+(\d+)/(\d+)\s+Retry\)", prompt_text, re.IGNORECASE)
            if m:
                self.record_attempt(
                    attempt=int(m.group(1)),
                    max_attempts=int(m.group(2)),
                    retry=int(m.group(3)),
                    max_retries=int(m.group(4)),
                )
            else:
                m_att = re.search(r"\(attempt\s+(\d+)\)", prompt_text, re.IGNORECASE)
                if m_att:
                    self.record_attempt(attempt=int(m_att.group(1)))

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
            wos = payload.get("metadata", {}).get("work_orders") or []
            wo_count_str = f" with {len(wos)} work orders" if wos else ""
            self.add_message(
                "system",
                f"[PLAN READY] Plan '{plan_id}' proposed{wo_count_str}. Type :approve (or 'continue') to authorize autonomous multi-agent execution, or :reject [feedback].",
            )

        elif name == "plan.approved":
            self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
            plan_id = payload.get("plan_id", "PLAN-001")
            self.completion_checklist["PLAN.md"] = True
            if self.plan:
                self.plan["state"] = "APPROVED"
                self.plan["approval_reason"] = payload.get("reason", "")
            if self.plan_revisions:
                self.plan_revisions[-1].state = "APPROVED"
            # Sync created work orders
            created_records = payload.get("created_records") or payload.get("work_orders")
            if isinstance(created_records, list) and created_records and isinstance(created_records[0], Mapping):
                self.sync_work_orders(created_records)
            first_role = self.work_orders[0].role if self.work_orders else "Backend"
            for r_name, r_obj in self.roles.items():
                if r_name.lower() in {"architecture", "lead"}:
                    r_obj.state = "RUNNING"
                elif r_name.lower() == first_role.lower():
                    r_obj.state = "RUNNING"
                else:
                    r_obj.state = "WAITING"
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
            r_raw = str(payload.get("role") or self._infer_role_from_op(agent_id)).strip()
            norm_lower = r_raw.lower()
            if norm_lower in ("qa", "q/a"):
                normalized_role = "Q/A"
            elif norm_lower == "gitops":
                normalized_role = "GitOps"
            elif norm_lower in ("architecture", "arch"):
                normalized_role = "Architecture"
            else:
                normalized_role = r_raw.title()
            existing_key = next((k for k in self.roles if k.lower() == normalized_role.lower()), None)
            target_key = existing_key or normalized_role
            existing_role = self.roles.get(target_key)
            raw_backend = payload.get("backend") or payload.get("executionBackend")
            if raw_backend and str(raw_backend).strip().lower() == "daemon":
                raw_backend = None
            backend = (
                raw_backend
                or (existing_role.backend if existing_role and existing_role.backend and existing_role.backend.lower() != "daemon" else "")
            )
            model = payload.get("model") or (existing_role.model if existing_role else None)
            quantization = payload.get("quantization") or getattr(existing_role, "quantization", None)
            wo_id = payload.get("work_order_id") or payload.get("workOrderId")
            parent_id = payload.get("parent_operation_id") or payload.get("parentOperationId") or "op-root"
            op_id = payload.get("operation_id") or payload.get("operationId") or f"op-{agent_id}"

            # Update roles
            self.roles[target_key] = RoleStatus(
                target_key,
                backend=backend,
                model=model,
                quantization=quantization,
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
            if wo_id:
                for wo in self.work_orders:
                    if wo.id == wo_id:
                        wo.status = "RUNNING"
            self.add_activity(normalized_role, "started", op_name or (wo_id or ""))

        elif name in {"operation.completed", "turn.completed"}:
            self.current_turn_actions.complete_all()
            self.current_turn_actions.expanded = False
            self.active_tool_name = None
            self.active_tool_target = None
            self.active_tool_code = None
            self.active_working_status = None
            self.reset_attempts_and_retries()
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
                if normalized_role.lower() in {"architecture", "lead"}:
                    if self.is_complete or self.phase == ProjectPhase.PROJECT_COMPLETE:
                        self.roles[normalized_role].state = "COMPLETED"
                    else:
                        self.roles[normalized_role].state = "RUNNING"
                else:
                    self.roles[normalized_role].state = "COMPLETED"
            if wo_id:
                for wo in self.work_orders:
                    if wo.id == wo_id:
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

            result = payload.get("result")
            if isinstance(result, Mapping):
                telem = result.get("telemetry")
                if isinstance(telem, Mapping):
                    p_tok = int(telem.get("prompt_tokens", 0) or 0)
                    c_tok = int(telem.get("completion_tokens", 0) or 0)
                    self.record_operation_tokens(op_id, prompt_tokens=p_tok, completion_tokens=c_tok)
            self.add_activity(normalized_role, "completed", op_name or (wo_id or ""))
            self.reconcile_roles_from_work_orders()

        elif name in {"operation.cancelled", "agent.cancelled", "turn.cancelled", "session.cancelled"}:
            self.active_working_status = None
            self.reset_attempts_and_retries()
            role = (payload.get("role") or "Agent").title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            op_id = payload.get("operation_id")
            if op_id and op_id in self.operations:
                self.operations[op_id].status = "CANCELLED"
            elif name == "session.cancelled":
                for op in self.operations.values():
                    if op.status.upper() in {"RUNNING", "ACTIVE"}:
                        op.status = "CANCELLED"
                for r in self.roles.values():
                    if r.state.upper() in {"RUNNING", "ACTIVE"}:
                        r.state = "CANCELLED"
            else:
                for op in self.operations.values():
                    if op.role == normalized_role and op.status == "RUNNING":
                        op.status = "CANCELLED"
            if normalized_role in self.roles and name != "session.cancelled":
                self.roles[normalized_role].state = "CANCELLED"
            self.add_activity(normalized_role, "cancelled", payload.get("reason", ""))

        elif name in {"operation.failed", "turn.failed"}:
            self.active_working_status = None
            op_name = payload.get("operation") or payload.get("name") or ""
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            op_id = payload.get("operation_id")
            from cli.tui.events import clean_error_message
            err_msg = clean_error_message(payload.get("error") or payload.get("reason") or "failed")
            if op_id and op_id in self.operations:
                self.operations[op_id].status = "FAILED"
            else:
                for op in self.operations.values():
                    if (op.role == normalized_role or (op_name and op.name == op_name)) and op.status == "RUNNING":
                        op.status = "FAILED"
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "FAILED"
            self.add_activity(normalized_role, "failed", str(err_msg))

        elif name in {"operation.blocked", "turn.blocked"}:
            self.active_working_status = None
            op_name = payload.get("operation") or payload.get("name") or ""
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            op_id = payload.get("operation_id")
            from cli.tui.events import clean_error_message
            block_msg = clean_error_message(payload.get("error") or payload.get("reason") or "blocked")
            if op_id and op_id in self.operations:
                self.operations[op_id].status = "BLOCKED"
            else:
                for op in self.operations.values():
                    if (op.role == normalized_role or (op_name and op.name == op_name)) and op.status == "RUNNING":
                        op.status = "BLOCKED"
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "BLOCKED"
            # Canonical blocker display: one entry per (failure_code, work
            # order, contract revision); raw evidence stays nested in the payload.
            self.add_activity(normalized_role, "blocked", canonical_blocker_text(block_msg), deduplicate=True)

        elif name.startswith("tool_call.") or name == "tool.call":
            tool_name = name.partition(".")[2] if "." in name else payload.get("tool", "tool")
            raw_role = payload.get("role") or payload.get("agent_id") or "Backend"
            role = "QA" if str(raw_role).lower() == "qa" else str(raw_role).title()
            target = (
                payload.get("path")
                or payload.get("file")
                or payload.get("test_path")
                or payload.get("test_name")
                or payload.get("selector")
                or payload.get("command")
                or payload.get("target")
                or payload.get("directory")
                or payload.get("dir_path")
                or payload.get("name")
                or payload.get("symbol")
                or ""
            )
            self.add_activity(role, tool_name, str(target))
            call_id = str(payload.get("call_id") or payload.get("operation_id") or f"call-{len(self.current_turn_actions.actions) + 1}")
            op_id = str(payload.get("operation_id") or "")
            parent_id = op_id if (op_id and call_id != op_id) else None
            from cli.tui.events import format_action_description, extract_tool_diff
            desc = format_action_description(tool_name, str(target), role=role if role != "Agent" else "")
            tool_code = None
            tool_diff = None
            if tool_name in {"write_file", "write", "create_file", "create"}:
                tool_code = str(payload.get("content") or payload.get("code") or "")
            elif tool_name in {"edit_file", "edit", "patch", "apply_patch"}:
                tool_diff = extract_tool_diff(payload, payload)
                if not tool_diff:
                    tool_code = str(payload.get("content") or payload.get("code") or "")
            self.record_turn_action(
                call_id, desc, status="RUNNING",
                tool_name=tool_name, target=str(target),
                code=tool_code, diff=tool_diff,
                parent_id=parent_id,
            )
            self.active_tool_name = tool_name
            self.active_tool_target = str(target)
            self.active_tool_code = tool_code
            self.active_tool_diff = tool_diff

        elif name == "event.toolCall":
            tool_name = payload.get("tool_name", "tool")
            raw_role = payload.get("role") or payload.get("agent_id") or "Agent"
            role = "QA" if str(raw_role).lower() == "qa" else str(raw_role).title()
            args_raw = payload.get("arguments")
            args = args_raw if isinstance(args_raw, Mapping) else {}
            target = (
                args.get("path")
                or args.get("file")
                or args.get("test_path")
                or args.get("test_name")
                or args.get("selector")
                or args.get("command")
                or args.get("target")
                or args.get("directory")
                or args.get("dir_path")
                or args.get("name")
                or payload.get("target")
                or ""
            )
            self.add_activity(role, tool_name, str(target))
            call_id = str(payload.get("call_id") or payload.get("operation_id") or f"call-{len(self.current_turn_actions.actions) + 1}")
            op_id = str(payload.get("operation_id") or "")
            parent_id = op_id if (op_id and call_id != op_id) else None
            from cli.tui.events import format_action_description, extract_tool_diff
            desc = format_action_description(tool_name, str(target), role=role if role != "Agent" else "")
            tool_code = None
            tool_diff = None
            if tool_name in {"write_file", "write", "create_file", "create"}:
                tool_code = str(args.get("content") or args.get("code") or "")
            elif tool_name in {"edit_file", "edit", "patch", "apply_patch"}:
                tool_diff = extract_tool_diff(payload, args)
                if not tool_diff:
                    tool_code = str(args.get("content") or args.get("code") or "")
            self.record_turn_action(
                call_id, desc, status="RUNNING",
                tool_name=tool_name, target=str(target),
                code=tool_code, diff=tool_diff,
                parent_id=parent_id,
            )
            self.active_tool_name = tool_name
            self.active_tool_target = str(target)
            self.active_tool_code = tool_code
            self.active_tool_diff = tool_diff

        elif name in {"event.toolResult", "tool.result"} or name.startswith("tool_result."):
            tool_name = payload.get("tool_name") or payload.get("tool") or "tool"
            role = (payload.get("role") or "Agent").title()
            status = payload.get("status", "completed")
            from cli.tui.events import clean_error_message, normalize_status, extract_tool_output, extract_tool_diff
            norm_status = normalize_status(status)
            self.add_activity(role, f"{tool_name} ({norm_status.lower()})", "")
            call_id = str(payload.get("call_id") or payload.get("operation_id") or "")
            err = clean_error_message(payload.get("error")) if payload.get("error") else None
            tool_output = extract_tool_output(payload)
            tool_diff = extract_tool_diff(payload)
            self.complete_turn_action(
                call_id, status=norm_status, error=err,
                tool_name=tool_name, output=tool_output, diff=tool_diff,
            )
            self.last_tool_name = self.active_tool_name or tool_name
            self.last_tool_target = self.active_tool_target
            self.last_tool_code = self.active_tool_code
            self.last_tool_diff = tool_diff or getattr(self, "active_tool_diff", None)
            self.last_tool_output = tool_output
            self.last_tool_status = norm_status
            self.active_tool_name = None
            self.active_tool_target = None
            self.active_tool_code = None
            self.active_tool_diff = None

        elif name in {"cycle.telemetry", "harness.telemetry"}:
            telem = payload.get("telemetry") or payload
            if isinstance(telem, Mapping):
                self.last_telemetry = dict(telem)
                self.cycle_telemetry.append(dict(telem))
                prof = telem.get("execution_profile") or payload.get("execution_profile")
                if prof:
                    self.execution_profile = str(prof)
                op_id = event.get("operation_id") or payload.get("operation_id")
                p_tok = int(telem.get("prompt_tokens", 0) or 0)
                c_tok = int(telem.get("completion_tokens", 0) or 0)
                self.record_operation_tokens(op_id, prompt_tokens=p_tok, completion_tokens=c_tok)

        elif name in {"token.usage", "usage.delta"}:
            op_id = event.get("operation_id") or payload.get("operation_id")
            p_tok = int(payload.get("prompt_tokens", 0) or 0)
            c_tok = int(payload.get("completion_tokens", 0) or 0)
            self.record_operation_tokens(op_id, prompt_tokens=p_tok, completion_tokens=c_tok)

        elif name in {"agent.thinking", "turn.thinking", "reasoning.delta"}:
            raw_think = payload.get("text") or payload.get("thinking") or payload.get("thought") or payload.get("reasoning") or payload.get("delta") or ""
            think_text = str(raw_think)
            if think_text.strip():
                # Preserve leading whitespace (including paragraph breaks)
                # Only strip trailing whitespace to avoid double-spaces
                chunk = think_text.rstrip()
                if chunk:
                    if getattr(self, "current_turn_thinking", None):
                        # Preserve leading whitespace for chunks like " how", " ,", " ."
                        # This maintains provider's original spacing and punctuation
                        if chunk.startswith((" ", "\t", "\n")):
                            self.current_turn_thinking += chunk
                        else:
                            self.current_turn_thinking += " " + chunk
                    else:
                        self.current_turn_thinking = chunk

        elif name in {"verification.completed", "verification.recorded"}:
            res = payload.get("result")
            if isinstance(res, Mapping):
                dims = res.get("dimensions") or res.get("verification_dimensions") or res
                for k, v in dims.items():
                    norm_k = str(k).lower().replace("_verified", "")
                    if norm_k in self.verification_dimensions:
                        self.verification_dimensions[norm_k] = bool(v)
            self.add_activity("StackMind", "recorded 6-D verification matrix")

        elif name in {"run.complete", "project.completed", "project.complete"}:
            self.phase = ProjectPhase.PROJECT_COMPLETE
            for k in self.completion_checklist:
                self.completion_checklist[k] = True
            self.is_complete = True
            self.reset_attempts_and_retries()
            for r_obj in self.roles.values():
                if r_obj.state.upper() in {"RUNNING", "ACTIVE", "WAITING"}:
                    r_obj.state = "COMPLETED"
            for op in self.operations.values():
                if op.status.upper() in {"RUNNING", "ACTIVE"}:
                    op.status = "COMPLETED"
            self.add_activity("StackMind", "delivered project complete handover")
            self.reconcile_roles_from_work_orders()

        elif name == "run.phase":
            phase_val = str(payload.get("phase", "")).upper()
            if phase_val == "COMPLETE":
                self.phase = ProjectPhase.PROJECT_COMPLETE
                for k in self.completion_checklist:
                    self.completion_checklist[k] = True
                self.is_complete = True
                self.error = None
                self.reset_attempts_and_retries()
                for r_obj in self.roles.values():
                    if r_obj.state.upper() in {"RUNNING", "ACTIVE", "WAITING"}:
                        r_obj.state = "COMPLETED"
                for op in self.operations.values():
                    if op.status.upper() in {"RUNNING", "ACTIVE"}:
                        op.status = "COMPLETED"
                self.add_activity("StackMind", "project complete")
                self.reconcile_roles_from_work_orders()
            elif phase_val in ("DISPATCHING", "EXECUTING", "AUTHORING", "INTEGRATION_REVIEW",
                               "AUTHORING_READINESS_GATE", "ARCHITECT_REPAIR", "ARCHITECT_RECOVERY_DECISION",
                               "PRODUCT_READY", "GITOPS"):
                self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                self.error = None
            elif phase_val == "AWAITING_APPROVAL":
                self.phase = ProjectPhase.AWAITING_APPROVAL
            elif phase_val == "BLOCKED":
                self.phase = ProjectPhase.BLOCKED
                if payload.get("error"):
                    from cli.tui.events import clean_error_message
                    self.error = clean_error_message(payload["error"])
                self.add_activity("StackMind", "run blocked", canonical_blocker_text(payload.get("error", "")), deduplicate=True)
                for r_obj in self.roles.values():
                    if r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                        r_obj.state = "WAITING"
            elif phase_val == "FAILED":
                self.phase = ProjectPhase.FAILED
                if payload.get("error"):
                    from cli.tui.events import clean_error_message
                    self.error = clean_error_message(payload["error"])
                self.add_activity("StackMind", "run failed", canonical_blocker_text(payload.get("error", "")), deduplicate=True)
                for r_obj in self.roles.values():
                    if r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                        r_obj.state = "WAITING"

        elif name == "run.blocked":
            self.phase = ProjectPhase.BLOCKED
            if payload.get("error"):
                from cli.tui.events import clean_error_message
                self.error = clean_error_message(payload["error"])
            self.add_activity("StackMind", "run blocked", canonical_blocker_text(payload.get("error", "")), deduplicate=True)
            for r_obj in self.roles.values():
                if r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                    r_obj.state = "WAITING"

        elif name == "run.failed":
            self.phase = ProjectPhase.FAILED
            if payload.get("error"):
                from cli.tui.events import clean_error_message
                self.error = clean_error_message(payload["error"])
            self.add_activity("StackMind", "run failed", canonical_blocker_text(payload.get("error", "")), deduplicate=True)
            for r_obj in self.roles.values():
                if r_obj.state.upper() in {"RUNNING", "ACTIVE"}:
                    r_obj.state = "WAITING"


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
