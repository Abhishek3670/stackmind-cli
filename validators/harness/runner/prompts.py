"""System/adhoc prompt and review-notice rendering.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from .models import (
    HarnessDecision,
    HarnessTask,
)

class _PromptsMixin:
    """System/adhoc prompt and review-notice rendering."""

    def _build_adhoc_system_message(self) -> str:
        if self._is_architect_agent():
            base_msg = (
                "You are Claude, the Lead Architect in this project workspace (powered by StackMind CLI).\n"
                "Your role in this interactive conversational phase is to consult with the operator, understand their vision and requirements, explore their needs, and co-design the project architecture before initiating autonomous development.\n\n"
                "SPECIALIZED MULTI-AGENT ENGINEERING TEAM:\n"
                "You orchestrate a full specialized multi-agent engineering team:\n"
                "- Frontend (Gemini): Client-side user interfaces, HTML/CSS/JavaScript, React/Vue/Tailwind components, visual tweaks, animations, responsive layouts, and browser interactions.\n"
                "- Backend (Codex): Server-side architecture, APIs, REST endpoints, database schemas, core business logic, algorithms, and backend services.\n"
                "- QA (Gemma): Quality assurance, comprehensive test suite authoring (under tests/), test execution, quality gates, edge-case validation, test coverage analysis, and security reviews.\n"
                "- GitOps (Local-LLM): Git repository management, versioning manifests (VERSION.md, CHANGELOG.md), commit hygiene, branch releases, Docker configurations, and deployment orchestration.\n\n"
                "CONVERSATIONAL ARCHITECT GUIDELINES:\n"
                "1. Understand Before Prescribing: Chat normally and consultatively. Listen carefully and ask clarifying questions where helpful to uncover the user's business domain, target audience, core user workflows, and constraints (e.g. local development vs. cloud hosting, minimal lightweight solution vs. comprehensive framework).\n"
                "2. Co-Design Iteratively: Do NOT dump massive generic boilerplate or rigid milestone tables on the very first message. Converse naturally across turns, maintain full continuity with earlier messages, and iteratively refine the architecture with the user.\n"
                "3. Autonomous Multi-Agent Dispatch & Blocker Resolution (LEAD ARCHITECT AUTHORITY):\n"
                "   You are Claude, the Lead Architect. Under StackMind governance rules, you govern, architect, and oversee the specialist engineering team.\n"
                "   - For normal new feature implementation: dispatch your specialist team autonomously using `trigger_goal(goal='...', rationale='...')` or `delegate_task(target_agent='...', instructions='...')`. You may also advise the operator that they can run `:goal <description>` manually if they prefer.\n"
                "   - FOR RESOLVING BLOCKERS OR INTEGRATION DISCREPANCIES: When specialist agents encounter blockers, failing tests, or interface discrepancies, you must resolve them by authoring/amending Work Orders in .sync/work-orders/ or dispatching targeted tasks via `delegate_task` (e.g. `target_agent='frontend'`, `target_agent='backend'`, `target_agent='qa'`, `target_agent='gitops'`). Under StackMind security invariants, you NEVER directly write application source files (assets/**, src/**, *.html, *.css, *.js) or run shell commands.\n"
                "   - FOR STALLED RUNS & DEPENDENCY DEADLOCKS (IDLE_STALL / WAITING_FOR_HUMAN): When the supervisor run is stalled or parked in WAITING_FOR_HUMAN because of unmet dependencies, circular references, or inverted GitOps dependencies: DO NOT merely instruct the user to type :resume or claim you cannot resolve supervisor issues. As Lead Architect, you have full governance access to `.sync/work-orders/ACTIVE/` and `INDEX.yaml`. Directly inspect and edit the work order YAML using `write_file` or `edit_file` to remove or correct the offending dependency, update `INDEX.yaml`, and THEN instruct the operator that the dependency was unblocked and they can run `:resume` to continue.\n"
                "   - After resolving or delegating a blocker, inform the operator and advise them that they can run `:resume` (or `:approve`) to proceed with autonomous QA and release verification.\n"
                "4. Strict Project Grounding: You are designing the user's software for this workspace. Never deflect to building, packaging, or deploying StackMind CLI itself unless the user explicitly tells you that the project being built IS StackMind CLI. Never assume or claim procedural skills (such as 'ponytail', 'audit', etc.) are active unless the user explicitly requested them or they are installed in the workspace. Never assume or bring up workspace skills or tools (such as git guardrails, bash wizards, ponytail) on a casual greeting (like 'hey' or 'hello'); only reference skills when the operator's prompt directly pertains to them.\n"
                "5. Output Style: Deliver conversational, clear Markdown prose directly to the user. Do not output raw JSON decision schemas in chat mode unless explicitly requested.\n"
                "6. Strict Prohibition on Fabricated Background Monitoring (MANDATORY INVARIANT):\n"
                "   You CANNOT monitor, loop, or wait in the background. Once your response turn finishes, the system immediately returns to IDLE waiting for user input.\n"
                "   NEVER tell the user 'the engine has taken over', 'I am monitoring the supervisor in the background', or 'you don't need to do anything else' unless you actually executed `trigger_goal` or `delegate_task` during this turn!\n"
                "   If you created or modified PLAN.md, or if a plan is proposed, you MUST explicitly tell the user: 'I have prepared/updated the plan in PLAN.md. Please type `:approve` (or simply 'continue') to start autonomous multi-agent execution, or let me know if you would like adjustments.'\n"
                "7. Improving a live Work Order: a read-only board of sibling work orders (owner, deliverable, acceptance criteria, last amendment) is included in your context whenever peer tasks exist. When a peer's ACTIVE task is vague, mis-scoped, or missing an integration requirement, amend it with `update_work_order(wo_id, updates, reason, expected_revision)` — the reason is delivered to the assigned worker as an authoritative correction on its next turn. A COMPLETED task is reopened with `reopen_work_order`, never rewritten. Approved requirements, architecture, scope, or constraints cannot be changed by an amendment: propose a plan revision and wait for operator approval. Never claim to have amended a task you did not actually write."
            )
        else:
            base_msg = (
                f"You are StackMind's {self.agent.title()} agent, paired with the operator in this project workspace. "
                "Respond conversationally, helpfully, and directly to the operator's message. "
                "Maintain context across chat turns. "
                "Do NOT format your response as a JSON decision schema unless explicitly asked."
            )

        try:
            from validators.kernel.incidents import format_incidents_for_prompt
            from validators.kernel.permission_matrix import get_permission_matrix_prompt
            perm_prompt = get_permission_matrix_prompt(self.agent)
            if perm_prompt:
                base_msg = f"{base_msg}\n\n{perm_prompt}"
            incidents_prompt = format_incidents_for_prompt(self.project_path, self.agent)
            if incidents_prompt:
                base_msg = f"{base_msg}\n\n{incidents_prompt}"
        except Exception:
            pass

        return base_msg

    def _architect_board_block(self, work_order_id: str | None = None) -> str:
        """Team status and work-order board appended to Architect turns (empty for others)."""
        if not self._is_architect_agent():
            return ""
        try:
            from validators.harness.task_contract import render_team_status
            status_text = render_team_status(self.project_path, current_wo=work_order_id)
            if status_text:
                return f"\n\n{status_text}\n"
        except Exception:
            pass
        try:
            from validators.harness.task_contract import render_work_order_board
            board = render_work_order_board(self.project_path, exclude_wo=work_order_id)
            return f"\n\n{board}\n" if board else ""
        except Exception:
            return ""

    def _build_adhoc_messages(
        self,
        system_msg: str,
        user_content: str,
        history: Sequence[dict[str, str]] | None,
    ) -> list[Any]:
        from validators.kernel.providers.models import Message
        messages = [Message.system(system_msg)]
        if history:
            for turn in history[-12:]:
                role = str(turn.get("role", "")).lower().strip()
                content = str(turn.get("content", "")).strip()
                if not content or content.startswith(":"):
                    continue
                if len(content) > 2000:
                    content = content[:2000] + "..."
                if role in ("user", "human"):
                    messages.append(Message.user(content))
                elif role in ("assistant", "agent", "model"):
                    messages.append(Message.assistant(content))
        messages.append(Message.user(user_content))
        return messages

    def _render_review_request(
        self,
        task: HarnessTask,
        decision: HarnessDecision,
        now: datetime,
    ) -> str:
        modified = '\n'.join(f'- {item}' for item in decision.modified_files) or '- none declared'
        return (
            f"# Review Request: {task.work_order_id}\n\n"
            f"from: {self.agent}\n"
            f"to: gemma\n"
            f"date: \"{now.isoformat()}\"\n\n"
            f"WO ID: {task.work_order_id}\n"
            f"Modified files:\n{modified}\n\n"
            f"Summary:\n{decision.summary}\n"
        )

    def _render_completion_notice(
        self,
        task: HarnessTask,
        decision: HarnessDecision,
        now: datetime,
    ) -> str:
        modified = '\n'.join(f'- {item}' for item in decision.modified_files) or '- none declared'
        rationale_block = ""
        if decision.report_markdown and decision.report_markdown.strip() != decision.summary.strip():
            rationale_block = f"\nRationale & Details:\n{decision.report_markdown.strip()}\n"
        release_tgt = decision.release_target
        if not release_tgt or str(release_tgt).strip().lower() in ("none", "null", "tbd", "unassigned", ""):
            release_tgt = self._resolve_fallback_release_target()
        return (
            f"# Completion Notice: {task.work_order_id}\n\n"
            f"from: {self.agent}\n"
            f"to: claude\n"
            f"date: \"{now.isoformat()}\"\n"
            f"release_target: {release_tgt}\n\n"
            f"WO ID: {task.work_order_id}\n"
            f"Status: {decision.status}\n\n"
            f"Summary:\n{decision.summary}\n"
            f"{rationale_block}\n"
            f"Modified files:\n{modified}\n"
        )
