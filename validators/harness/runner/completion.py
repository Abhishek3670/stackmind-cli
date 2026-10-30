"""LLM request assembly and completion handling (_complete_request).

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import (
    QA_SYSTEM_PROMPT,
    CompletionRecord,
    EchoLLMProvider,
    GovernedToolRuntime,
    LLMRequest,
)

class _CompletionMixin:
    """LLM request assembly and completion handling (_complete_request)."""

    def _complete_request(
        self, request: LLMRequest, tool_runtime: GovernedToolRuntime | None,
    ) -> CompletionRecord:
        """Run legacy completions or the existing ProviderGateway tool loop."""
        if tool_runtime is None:
            if (
                self.provider_adapter is not None
                and (
                    not hasattr(self.llm_provider, 'complete')
                    or isinstance(self.llm_provider, EchoLLMProvider)
                )
            ):
                from validators.kernel.providers.models import Message
                if request.task.kind == 'adhoc':
                    system_msg = self._build_adhoc_system_message()
                    user_content = (request.task.body or request.task.title).strip()
                    board_block = self._architect_board_block(
                        getattr(request.task, 'work_order_id', None)
                    )
                    if board_block:
                        system_msg = f"{system_msg}\n{board_block}"
                    if getattr(request.context, "text", None) and request.context.text.strip():
                        system_msg = (
                            f"{system_msg}\n\n"
                            "WORKSPACE CONTEXT & AVAILABLE SKILLS (Background Reference Only - do not assume operator requested these unless explicitly mentioned in prompt):\n"
                            f"{request.context.text.strip()}\n"
                        )
                    messages = self._build_adhoc_messages(system_msg, user_content, request.session_history)
                else:
                    task_text = f'Task: {request.task.title}\n\n{request.task.body}\n\n'
                    prompt_text = f"{task_text}Context:\n{getattr(request.context, 'text', '')}"
                    messages = [Message.user(prompt_text)]
                response = self.provider_adapter.complete(
                    messages,
                    cancellation_token=request.cancellation,
                )
                raw_text = (response.message.content or '').strip()
                return CompletionRecord(
                    provider=getattr(self.provider_adapter, 'provider_name', self.backend_id),
                    model=getattr(self.provider_adapter, 'model_name', self.backend_model),
                    payload={
                        'status': 'completed',
                        'summary': raw_text,
                        'report_markdown': raw_text,
                        'blockers': [],
                        'modified_files': [],
                        'release_target': (request.task.deliverable_path or 'patch') if request.task.work_order_id else None,
                        'commands': [],
                        'retrieval_queries': [],
                        'uncertainty': [],
                    },
                    prompt_tokens=response.usage.prompt_tokens if getattr(response, "usage", None) else 0,
                    completion_tokens=response.usage.completion_tokens if getattr(response, "usage", None) else 0,
                )
            return self.llm_provider.complete(request)

        from validators.kernel.providers.gateway import (
            ProviderGateway,
            get_curated_tools_for_phase,
        )
        from validators.kernel.providers.models import Message
        from validators.kernel.session import Attempt

        kernel_contract = tool_runtime.gateway.contract
        attempt = Attempt(tool_runtime.workspace.attempt_id, kernel_contract)
        task_text = f'Task: {request.task.title}\n\n{request.task.body}\n\n'
        full_task_desc = f"{request.task.title}\n{request.task.body}"
        is_architecture = self._is_architect_agent()
        is_authoring_task = (
            is_architecture
            and any(
                kw in full_task_desc.lower()
                for kw in (
                    "author the implementation work orders",
                    "author child work orders",
                    "has been approved by the operator",
                    "authoring work orders and contracts",
                )
            )
        )
        is_integration_review = (
            is_architecture
            and any(
                kw in full_task_desc.lower()
                for kw in (
                    "integration review",
                    "final integration review",
                    "perform integration review",
                )
            )
        )
        is_delivery_status = (
            is_architecture
            and (
                getattr(request.task, 'is_delivery_status', False)
                or getattr(request, 'is_delivery_status', False)
                or request.task.kind in ('delivery_status', 'product_delivery_status')
                or any(
                    kw in full_task_desc.lower()
                    for kw in (
                        "product delivery status",
                        "delivery status",
                        "provide the product delivery status",
                        "product delivery report",
                    )
                )
            )
        )
        is_plan_task = (
            not is_delivery_status
            and
            not is_authoring_task
            and not is_integration_review
            and (
                request.task.deliverable_path == 'PLAN.md'
                or 'PLAN.md' in request.task.title
                or 'PLAN.md' in (request.task.body or '')
            )
        )
        task_phase = (
            "planning" if is_plan_task
            else "authoring" if is_authoring_task
            else "integration" if is_integration_review
            else "delivery_status" if is_delivery_status
            else None
        )
        uses_task_contract = False
        gateway = ProviderGateway(
            self.provider_adapter,
            tool_runtime.gateway,
            attempt=attempt,
            contract=kernel_contract,
            tools=get_curated_tools_for_phase(
                self.agent,
                phase=task_phase,
                backend_id=self.backend_id,
            ),
            on_tool_call=getattr(self, 'on_tool_call', None),
            on_tool_result=getattr(self, 'on_tool_result', None),
            on_thinking=getattr(self, 'on_thinking', None),
            on_token=getattr(self, 'on_token', None),
            on_usage=getattr(self, 'on_usage', None),
        )
        if is_plan_task:
            from validators.harness.plan import PLAN_GENERATION_INSTRUCTIONS
            task_text += f"\nImportant PLAN.md formatting requirement:\n{PLAN_GENERATION_INSTRUCTIONS}\n"

        if is_architecture and is_plan_task:
            from validators.harness.plan import (
                ARCHITECTURE_RESEARCH_INSTRUCTIONS,
                CONTRACT_SCHEMA_TEMPLATE,
                WORK_ORDER_SCHEMA_TEMPLATE,
            )
            from validators.skill.selector import format_skills_for_architect_prompt
            task_text += f"\n{ARCHITECTURE_RESEARCH_INSTRUCTIONS}\n"
            task_text += f"\nGoverned Artifact Authoring Schemas (for reference when planning tasks):\n"
            task_text += f"{WORK_ORDER_SCHEMA_TEMPLATE}\n{CONTRACT_SCHEMA_TEMPLATE}\n"
            t_title = getattr(request.task, 'title', '') or ''
            t_desc = getattr(request.task, 'body', '') or getattr(request.task, 'query', '') or getattr(request.context, 'text', '') or ''
            skills_prompt = format_skills_for_architect_prompt(
                self.project_path,
                task_title=t_title,
                task_description=t_desc,
            )
            if skills_prompt:
                task_text += f"\n{skills_prompt}\n"

        if is_architecture and is_authoring_task:
            from validators.harness.plan import (
                CONTRACT_SCHEMA_TEMPLATE,
                WORK_ORDER_SCHEMA_TEMPLATE,
            )
            from validators.skill.selector import format_skills_for_architect_prompt
            task_text += f"\nGoverned Artifact Authoring Schemas (REQUIRED for writing child work orders and contracts):\n"
            task_text += f"{WORK_ORDER_SCHEMA_TEMPLATE}\n{CONTRACT_SCHEMA_TEMPLATE}\n"
            t_title = getattr(request.task, 'title', '') or ''
            t_desc = getattr(request.task, 'body', '') or getattr(request.task, 'query', '') or getattr(request.context, 'text', '') or ''
            skills_prompt = format_skills_for_architect_prompt(
                self.project_path,
                task_title=t_title,
                task_description=t_desc,
            )
            if skills_prompt:
                task_text += f"\n{skills_prompt}\n"

        if is_architecture and is_integration_review:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI and the FINAL GATEKEEPER before release. "
                "All implementation work orders and QA verifications have completed. "
                "Your task is to perform the final integration review of all declared deliverables against the original product goal. "
                "You are solely accountable for ensuring that the assembled deliverables fully satisfy the product goal and integrate cleanly.\n"
                "You operate in a governed environment with a strictly read-only contract: use `read_file` or `list_directory` to inspect deliverables. "
                "Do NOT attempt to write or edit any files.\n"
                "INTEGRATION REVIEW CHECKLIST (MUST VERIFY BEFORE APPROVING):\n"
                "1. Product Goal Fulfillment: Inspect the actual content of the files. If the user goal requested specific features, text, or entities, verify they actually exist in the rendered markup/output — not just placeholder scaffolding.\n"
                "2. Cross-File Integration: Verify that element IDs, classes, and function references align across files (e.g. if script.js queries an element ID or style.css targets a class, that ID/class MUST exist in index.html).\n"
                "3. Release Packaging & Documentation: Release packaging manifests (CHANGELOG.md, VERSION.md) and project README are finalized and governed by the supervisor and GitOps in the release phase. If CHANGELOG.md lacks 0.1.0 release notes or README is boilerplate, do NOT block code integration review when core deliverables pass QA.\n"
                "4. Board & Runtime Retries: WO-000 is the root planning order and is governed by the supervisor. Historical worker timeouts that subsequently succeeded on retry with passing QA suites are normal self-healing events and must NOT block release.\n"
                "5. NO RUBBER-STAMPING FOR CODE DEFECTS: If core code files are disconnected, placeholder-only, or fail QA, you MUST declare status 'blocked'. Populate 'blockers' with concise summaries and 'blocker_details' with [{'finding': '...', 'remediation': '...', 'path': '...'}]. The supervisor will automatically route the prescribed rework back to the appropriate developer to fix it before release.\n"
                "6. Context & Environment: Check developer completion notices in .sync/inbox/claude/ for implementation rationales. In development environments, local developer conveniences (like debug mode and dev secret fallbacks) are permissible and do not block release.\n"
                "7. When your review is complete, return your final decision strictly as JSON conforming to the requested schema."
            )
        elif is_architecture and is_authoring_task:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI. "
                "The operator has approved the architecture plan in PLAN.md. "
                "Your task now is to author the implementation Work Orders and Contracts for the tasks in PLAN.md. "
                "You operate in a governed environment where all file I/O MUST be performed through provided tools (read_file, write_file).\n"
                "WORKFLOW RULE: Call `write_file` to write each Work Order YAML to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
                "and each Contract YAML to .sync/contracts/<WO-ID>.yaml conforming to the provided schemas.\n"
                "INTEGRATION CONTRACT RULE: When decomposing a feature across multiple files (e.g. HTML, CSS, JS), "
                "the parent Work Order (e.g. index.html) MUST explicitly declare the exact markup, container classes, and "
                "element IDs (e.g. #animated-name) in its deliverable description and acceptance criteria so downstream "
                "scripts and styles hook into existing elements. Never leave parent deliverables as empty placeholder scaffolding.\n"
                "FILE OWNERSHIP RULE: In each Work Order's description and acceptance_criteria, you must ONLY instruct the worker to create or modify the specific file declared in its own `deliverable.path`. Never instruct a worker to initialize, create, or stub files that are the assigned deliverable of another work order (e.g. do not tell a scaffolding worker to touch index.html or main.js if those are deliverables of subsequent work orders), as runtime contract policies will deny out-of-scope file operations.\n"
                "TASK AMENDMENT RULE: To sharpen an existing ACTIVE work order, call `update_work_order(wo_id, updates, reason, expected_revision)` — or rewrite it with `write_file` after incrementing its `revision` by one and appending an `amendments` entry that states the reason. A write that keeps or lowers `revision` is rejected as an overwrite conflict, and workers can never amend. Your amendment is handed to the assigned worker as an authoritative correction on its next turn, so make the reason a concrete instruction (the missing hook, the wrong path, the acceptance criterion to add).\n"
                "COMPLETED WORK ORDER RULE: never rewrite a COMPLETED work order in place. Call `reopen_work_order(wo_id, reason, expected_revision)` — the lifecycle transition preserves the old completion record for audit, increments the revision, and marks prior verification evidence stale so the work must be re-verified. A higher revision is never authorization to bypass this.\n"
                "GOVERNANCE CHANGE RULE: an amendment may not change `requirement_refs`, `plan_ref`, `scope`, `constraints`, or the work-order `type`. Those alter user-approved requirements, architecture, interfaces, or immutable constraints: update PLAN.md, obtain operator approval through the existing propose/approve plan flow, then re-author the affected work orders. Until that approval exists the current approved contract stands.\n"
                "Only worker roles may be assigned to implementation work orders (e.g. codex for backend, gemini for frontend, gemma for qa, local-llm for gitops). "
                "Architects must NEVER assign implementation tasks to claude.\n"
                "When all work orders and contracts are written, return the final HarnessDecision as JSON."
            )
        elif is_architecture and is_delivery_status:
            system_msg = (
                "You are Claude, the Senior Architect and Technical Lead for StackMind CLI. "
                "Autonomous product delivery is now COMPLETE: all implementation work orders have been executed, "
                "QA-verified by Gemma, integration-reviewed by you, and versioned/released by GitOps.\n\n"
                "Your objective is to present the official PRODUCT DELIVERY STATUS report to the user / CEO.\n\n"
                "PRODUCT DELIVERY STATUS PROMPT FEATURE RULES:\n"
                "1. Executive Goal Fulfillment: State clearly that autonomous product delivery is complete and summarize how the delivered solution fulfills the original product goal.\n"
                "2. Architecture & Components Delivered: Provide a comprehensive breakdown of architectural components, modules, and key files delivered across all completed milestones/work orders (by Backend, Frontend, QA, and GitOps).\n"
                "3. Release & Versioning Manifest: Cite the released version (from VERSION.md), changelog highlights (from CHANGELOG.md), and Git commit SHA.\n"
                "4. Quality Assurance & Verification: Summarize QA test suite results, security scan status, and integration review validation.\n"
                "5. Getting Started & Execution Guide: Provide clear, actionable instructions for the user on how to run, test, or launch the delivered application.\n"
                "6. Format: Present your report directly in clean, well-structured Markdown conforming to the standard Product Delivery Status layout. Return the final HarnessDecision JSON declaring status 'completed'."
            )
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
            task_text = (request.task.body or request.task.title).strip()
            task_text += (
                "\n\nINSTRUCTIONS: Formulate and present the official Product Delivery Status report now, adhering strictly "
                "to the Product Delivery Status prompt feature rules. Include executive goal fulfillment, architecture & components, "
                "release manifest, verification proof, and getting started instructions.\n"
            )
        elif request.task.kind == 'adhoc':
            system_msg = self._build_adhoc_system_message()
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
            task_text = (request.task.body or request.task.title).strip()
        elif is_architecture:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI. "
                "You are responsible for codebase research, architecture planning, and decomposing product goals into actionable work orders and contracts. "
                "You decide which specialist agent (codex for backend, gemini for frontend, gemma for qa, local-llm for gitops) executes each milestone, indicating `(Agent: <agent>)` in each milestone title. "
                "Specialist agents autonomously acquire optimum procedural skills directly from their dedicated role libraries based on work orders. You may focus PLAN.md on clear milestones, deliverables, and interface contracts, optionally specifying `(Skills: <skill_name>)` only when an explicit procedural override is needed. "
                "You operate in a governed environment where all file I/O, graph queries, and web research MUST be performed through provided tools (query_graph, read_file, write_file, web_search, web_fetch).\n"
                "WORKFLOW RULE: In your first action, you MUST call the `query_graph` tool to inspect existing project architecture. "
                "Only after receiving the graph research results should you call `write_file` to write PLAN.md.\n"
                "SYSTEM INTEGRATION RULE: Plan end-to-end integration: if a milestone produces frontend assets (HTML/CSS/JS), "
                "sequence them with explicit interface and DOM contracts so the assembled page delivers every element and text string requested in the product goal.\n"
                "Return the final HarnessDecision as JSON."
            )
            try:
                from validators.kernel.incidents import format_incidents_for_prompt
                from validators.kernel.permission_matrix import get_permission_matrix_prompt
                perm_prompt = get_permission_matrix_prompt(self.agent)
                if perm_prompt:
                    system_msg = f"{system_msg}\n\n{perm_prompt}"
                incidents_prompt = format_incidents_for_prompt(self.project_path, self.agent)
                if incidents_prompt:
                    system_msg = f"{system_msg}\n\n{incidents_prompt}"
            except Exception:
                pass
        elif self.agent in ('gemma', 'qa'):
            system_msg = QA_SYSTEM_PROMPT
            try:
                from validators.kernel.incidents import format_incidents_for_prompt
                from validators.kernel.permission_matrix import get_permission_matrix_prompt
                perm_prompt = get_permission_matrix_prompt(self.agent)
                if perm_prompt:
                    system_msg = f"{system_msg}\n\n{perm_prompt}"
                incidents_prompt = format_incidents_for_prompt(self.project_path, self.agent)
                if incidents_prompt:
                    system_msg = f"{system_msg}\n\n{incidents_prompt}"
            except Exception:
                pass
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
        elif self.agent in ('local-llm', 'gitops'):
            system_msg = (
                "You are Local-LLM, the GitOps & Release Lead for StackMind CLI. "
                "You are responsible for versioning hygiene, release documentation, and changelog maintenance. "
                "The supervisor automatically creates the git commit upon completion. "
                "Workflow instructions:\n"
                "1. If VERSION.md does not exist, use `write_file` to write VERSION.md (e.g. '0.1.0').\n"
                "2. If CHANGELOG.md does not exist, use `write_file` to write CHANGELOG.md documenting the release deliverables.\n"
                "3. Immediately return the final HarnessDecision JSON declaring status 'completed'."
            )
            try:
                from validators.kernel.incidents import format_incidents_for_prompt
                from validators.kernel.permission_matrix import get_permission_matrix_prompt
                perm_prompt = get_permission_matrix_prompt(self.agent)
                if perm_prompt:
                    system_msg = f"{system_msg}\n\n{perm_prompt}"
                incidents_prompt = format_incidents_for_prompt(self.project_path, self.agent)
                if incidents_prompt:
                    system_msg = f"{system_msg}\n\n{incidents_prompt}"
            except Exception:
                pass
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
        else:
            # Worker turns run against a COMPILED task contract: the same
            # structured sources the gates read, rendered as one JSON block —
            # ownership, budget, acceptance criteria, and the decision output
            # contract.  Replaces the prose task/scope paragraphs.
            from validators.harness.task_contract import (
                compile_worker_task_contract,
                render_task_block,
                render_worker_system,
            )
            wo_data = None
            contract_data = None
            if request.task.work_order_id:
                for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
                    wo_path = self.sync_path / 'work-orders' / sub / f'{request.task.work_order_id}.yaml'
                    if wo_path.exists():
                        wo_data = self._read_yaml(wo_path)
                        break
                contract_path = self.sync_path / 'contracts' / f'{request.task.work_order_id}.yaml'
                if contract_path.exists():
                    contract_data = self._read_yaml(contract_path)
            task_contract_json = compile_worker_task_contract(
                request.task, wo_data, contract_data, self.project_path,
            )
            system_msg = render_worker_system(task_contract_json)
            try:
                from validators.kernel.incidents import format_incidents_for_prompt
                from validators.kernel.permission_matrix import get_permission_matrix_prompt
                perm_prompt = get_permission_matrix_prompt(self.agent)
                if perm_prompt:
                    system_msg = f"{system_msg}\n\n{perm_prompt}"
                incidents_prompt = format_incidents_for_prompt(self.project_path, self.agent)
                if incidents_prompt:
                    system_msg = f"{system_msg}\n\n{incidents_prompt}"
            except Exception:
                pass
            task_text = render_task_block(task_contract_json) + '\n'
            uses_task_contract = True
            if tool_runtime is not None:
                from validators.harness.task_contract import TaskOwnership
                try:
                    tool_runtime.gateway.task_ownership = TaskOwnership.from_contract(task_contract_json)
                except Exception:
                    pass

            existing_files: list[str] = []
            try:
                target_dir = tool_runtime.workspace.root if tool_runtime is not None else self.project_path
                for p in target_dir.rglob("*"):
                    if p.is_file() and not any(part in (".git", ".sync", "__pycache__", ".venv", "node_modules") for part in p.parts):
                        existing_files.append(p.relative_to(target_dir).as_posix())
            except Exception:
                pass
            files_context = ""
            if existing_files:
                files_context = f"\nExisting workspace files:\n" + "\n".join(f"- {f}" for f in sorted(existing_files)[:30]) + "\n"

            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = f"{raw_context}\n{files_context}" if raw_context else files_context

        if request.task.kind == 'adhoc':
            user_content = (request.task.body or request.task.title).strip()
            board_block = self._architect_board_block(getattr(request.task, 'work_order_id', None))
            if board_block:
                system_msg = f"{system_msg}\n{board_block}"
            if getattr(request.context, "text", None) and request.context.text.strip():
                system_msg = (
                    f"{system_msg}\n\n"
                    "WORKSPACE CONTEXT & AVAILABLE SKILLS (Background Reference Only - do not assume operator requested these unless explicitly mentioned in prompt):\n"
                    f"{request.context.text.strip()}\n"
                )
            messages = self._build_adhoc_messages(system_msg, user_content, request.session_history)
        else:
            # Pull-based sibling state: the Architect gets the same board the
            # gates read, so peer tasks can be critiqued/amended from evidence
            # instead of guesswork. Live turns remain invisible by design.
            task_text += self._architect_board_block(getattr(request.task, 'work_order_id', None))

            if request.task.deliverable_path and not is_architecture:
                task_text += (
                    f"\nDELIVERABLE INSTRUCTION:\nYou MUST create or update '{request.task.deliverable_path}' by calling the `write_file` tool. "
                    "Do NOT just explain your plan or describe the solution in plain text. You must call `write_file` directly to author the code into the file. "
                    "INTEGRATION REQUIREMENT: Ensure your deliverable integrates with other workspace files (e.g. element IDs in JS matching HTML, classes in CSS matching markup). If your deliverable references an ID or class from a companion file in scope, make sure it matches.\n"
                )

            user_content = f'{task_text}Context:\n{full_context if not is_architecture else getattr(request.context, "text", "")}'
            messages = [
                Message.system(system_msg),
                Message.user(user_content),
            ]
        from validators.kernel.providers.errors import (
            ConsecutiveToolFailureError,
            NoProgressLoopError,
            ToolLimitExceededError,
            ToolLoopExhaustedError,
        )

        guard_trip: str | None = None
        decision_source: str = "model"

        try:
            history = gateway.run_loop(
                messages,
                max_turns=self.max_turns,
                max_tool_calls=self.max_tool_calls,
                timeout=self.turn_timeout,
                cancellation_token=request.cancellation,
                required_deliverable=request.task.deliverable_path if not is_architecture else None,
            )
        except (
            ToolLimitExceededError,
            ToolLoopExhaustedError,
            NoProgressLoopError,
            ConsecutiveToolFailureError,
        ) as exc:
            guard_trip = type(exc).__name__
            # Check what files have been written so far
            written_files_so_far: list[str] = list(getattr(gateway, "written_files", []))
            if not written_files_so_far:
                failed_call_ids: set[str] = set()
                for msg in messages:
                    if getattr(msg, 'role', '') == 'tool':
                        content_str = str(msg.content or '')
                        if content_str.startswith("Error:") or content_str.startswith("Warning:") or "PermissionError" in content_str:
                            if getattr(msg, 'tool_call_id', None):
                                failed_call_ids.add(msg.tool_call_id)
                for msg in messages:
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            if tc.id and tc.id in failed_call_ids:
                                continue
                            tc_args = getattr(tc, 'arguments', None) or {}
                            if tc.name == 'write_file' and 'path' in tc_args:
                                p = str(tc_args['path'])
                                if p not in written_files_so_far:
                                    written_files_so_far.append(p)

            # Hard-block only if there are no writes (for writing roles)
            is_non_writing_role = (self.agent in ('gemma', 'qa', 'claude')) or (
                self.agent in ('local-llm', 'gitops') and (
                    (self.project_path / "VERSION.md").is_file() or (self.project_path / "CHANGELOG.md").is_file()
                )
            )
            if not written_files_so_far and not is_non_writing_role:
                raise

            # Exactly one tool-less finalization turn
            fin_prompt = (
                f"Execution guard '{guard_trip}' was triggered: {exc}. "
                "Tool execution is now closed. Based on the files you have written, "
                "please output your final HarnessDecision JSON now."
            )
            messages.append(Message.user(fin_prompt))
            try:
                fin_resp = gateway.adapter.complete(messages, tools=[])
                gateway._record_and_check_budget(fin_resp.usage)
                messages.append(fin_resp.message)
            except Exception:
                pass
            history = messages

        assistant_msgs = [m for m in history if m.role == 'assistant']
        if not assistant_msgs:
            if guard_trip:
                assistant_msgs = [Message.assistant("")]
            else:
                raise ValueError('provider tool loop ended without an assistant message')
        final = next((m for m in reversed(assistant_msgs) if m.content and m.content.strip()), assistant_msgs[-1])
        raw_text = (final.content or '').strip()
        if request.task.kind == 'adhoc' and not raw_text and getattr(gateway, 'adapter', None) is not None:
            try:
                fin_messages = [
                    m for m in history
                    if (m.content and m.content.strip()) or m.tool_calls or m.role != 'assistant'
                ]
                fin_prompt = (
                    "Based on the tool results and workspace inspection above, please provide your final, "
                    "comprehensive conversational response to the operator's message."
                )
                fin_messages.append(Message.user(fin_prompt))

                streamed_parts: list[str] = []
                if hasattr(gateway.adapter, 'stream'):
                    try:
                        for chunk in gateway.adapter.stream(fin_messages, tools=[]):
                            if getattr(chunk, 'delta_content', None):
                                streamed_parts.append(chunk.delta_content)
                                # Propagate delta through on_token callback (set by SessionManager for daemon delivery)
                                if self.on_token:
                                    try:
                                        self.on_token(chunk.delta_content)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                if streamed_parts:
                    raw_text = "".join(streamed_parts).strip()

                if not raw_text:
                    fin_resp = gateway.adapter.complete(fin_messages, tools=[])
                    if fin_resp and getattr(fin_resp, 'message', None) and fin_resp.message.content:
                        raw_text = fin_resp.message.content.strip()
                        if self.on_token and raw_text:
                            try:
                                self.on_token(raw_text)
                            except Exception:
                                pass
            except Exception:
                pass
        payload = self._parse_json_payload(raw_text)

        written_files: list[str] = list(getattr(gateway, "written_files", []))
        if not written_files:
            failed_call_ids: set[str] = set()
            for msg in history:
                if getattr(msg, 'role', '') == 'tool':
                    content_str = str(msg.content or '')
                    if content_str.startswith("Error:") or content_str.startswith("Warning:") or "PermissionError" in content_str:
                        if getattr(msg, 'tool_call_id', None):
                            failed_call_ids.add(msg.tool_call_id)
            for msg in history:
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.id and tc.id in failed_call_ids:
                            continue
                        tc_args = getattr(tc, 'arguments', None) or {}
                        if tc.name == 'write_file' and 'path' in tc_args:
                            p = str(tc_args['path'])
                            if p not in written_files:
                                written_files.append(p)

        # Executed-tool audit for the governed loop: deterministic evidence of
        # what the model actually ran (e.g. run_tests for QA work orders).
        tool_calls_audit: list[dict[str, Any]] = []
        for msg in history:
            for tc in getattr(msg, 'tool_calls', ()) or ():
                entry: dict[str, Any] = {'tool': tc.name}
                tc_args = getattr(tc, 'arguments', None) or {}
                for key in ('path', 'command', 'argv', 'cmd', 'pattern'):
                    if key in tc_args:
                        entry[key] = tc_args[key]
                tool_calls_audit.append(entry)

        if request.task.kind == 'adhoc' and not raw_text:
            inspected_items = []
            for tc in tool_calls_audit:
                tname = tc.get("tool", "")
                arg_str = ""
                for k in ('path', 'file', 'target', 'command', 'symbol', 'query'):
                    if k in tc:
                        arg_str = str(tc[k])
                        break
                inspected_items.append(f"- `{tname}` {arg_str}".strip())
            tools_summary = "\n".join(inspected_items) if inspected_items else "Workspace tools executed."
            orig_prompt = request.task.body or request.task.title or "your request"

            if is_architecture:
                raw_text = (
                    f"### Workspace Exploration Summary\n"
                    f"I have inspected the project workspace:\n{tools_summary}\n\n"
                    f"### Architectural Recommendation\n"
                    f"To turn this plan into code, launch the app in the development environment, "
                    f"and execute verification across the specialist agent team, run:\n\n"
                    f"`:goal {orig_prompt}`\n\n"
                    f"This will initialize milestones in PLAN.md, assign work orders, and verify delivery."
                )
            else:
                raw_text = (
                    f"Completed workspace inspection:\n{tools_summary}\n\n"
                    f"To execute this goal across the autonomous agent team, run:\n`:goal {orig_prompt}`"
                )
            if self.on_token and raw_text:
                try:
                    self.on_token(raw_text)
                except Exception:
                    pass
            payload = self._parse_json_payload(raw_text)

        # Plan deliverable fallback: if is_plan_task and PLAN.md was not written via tool call,
        # extract it from raw text / assistant messages / payload report and persist it to staged root.
        if is_plan_task and not any(Path(p).name == "PLAN.md" for p in written_files):
            from validators.harness.plan import extract_plan_from_text, validate_plan_structure
            staged_plan = (
                tool_runtime.workspace.root / "PLAN.md"
                if tool_runtime and getattr(tool_runtime, "workspace", None)
                else self.project_path / "PLAN.md"
            )
            candidates = [raw_text]
            if isinstance(payload, dict):
                if payload.get("report_markdown"):
                    candidates.append(str(payload["report_markdown"]))
                if payload.get("summary"):
                    candidates.append(str(payload["summary"]))
            for msg in reversed(assistant_msgs):
                if getattr(msg, "content", None) and msg.content.strip():
                    candidates.append(msg.content.strip())

            for c_text in candidates:
                extracted = extract_plan_from_text(
                    c_text, default_title=request.task.title or "Project Architecture"
                )
                if extracted:
                    is_val, _ = validate_plan_structure(extracted)
                    if is_val:
                        staged_plan.parent.mkdir(parents=True, exist_ok=True)
                        staged_plan.write_text(extracted, encoding="utf-8")
                        if "PLAN.md" not in written_files:
                            written_files.append("PLAN.md")
                        break

        if not isinstance(payload, dict):
            if guard_trip:
                decision_source = "synthesized_after_loop_guard"

            # Check if this was an unfulfilled writing task
            is_unfulfilled_deliverable = False
            if request.task.deliverable_path and not written_files:
                deliverable_exists = False
                try:
                    target_dir = tool_runtime.workspace.root if tool_runtime is not None else self.project_path
                    deliverable_exists = (target_dir / request.task.deliverable_path).exists()
                except Exception:
                    pass
                if not deliverable_exists:
                    is_unfulfilled_deliverable = True

            wo_type_upper = str(getattr(request.task, 'work_order_type', '') or '').upper()
            is_validation_or_review = (
                wo_type_upper == 'VALIDATION'
                or request.task.kind == 'validation'
                or (wo_type_upper not in ('RESEARCH', 'AUDIT') and (
                    'integration review' in (request.task.title or '').lower()
                    or 'validation review' in (request.task.title or '').lower()
                    or 'review' in (request.task.title or '').lower()
                    or 'validation' in (request.task.title or '').lower()
                ))
            )

            # If raw_text contains unparsed JSON-like text with report_markdown or summary, extract clean text
            extracted_report = None
            extracted_summary = None
            if raw_text:
                for key in ("report_markdown", "summary", "response", "review_notes"):
                    m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text, re.DOTALL)
                    if m:
                        val = m.group(1).replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                        if val.strip() and val.strip() != "Completed task via tools":
                            if key == "report_markdown":
                                extracted_report = val.strip()
                            elif not extracted_summary:
                                extracted_summary = val.strip()

            effective_raw_text = extracted_report or raw_text

            if is_unfulfilled_deliverable:
                payload = {
                    'status': 'blocked',
                    'summary': f"Worker ended turn without authoring declared deliverable '{request.task.deliverable_path}': {effective_raw_text[:200]}",
                    'report_markdown': f"Worker ended turn without authoring declared deliverable '{request.task.deliverable_path}'. Raw output: {effective_raw_text}",
                    'modified_files': written_files,
                    'blockers': [f"declared deliverable '{request.task.deliverable_path}' was not added or modified in this turn"],
                }
            elif is_validation_or_review:
                blocker_detail = effective_raw_text[:200] if effective_raw_text else (f"Validation tripped loop guard: {guard_trip}" if guard_trip else "Validation review ended without structured approval")
                payload = {
                    'status': 'blocked',
                    'summary': f"Validation review requires resolution: {blocker_detail}",
                    'report_markdown': effective_raw_text or f"Validation review ended without structured approval ({guard_trip or 'fallback'}).",
                    'modified_files': written_files,
                    'blockers': [blocker_detail],
                }
            else:
                summary_text = extracted_summary or (effective_raw_text if ((request.task.kind == 'adhoc' or is_delivery_status) and effective_raw_text) else (effective_raw_text[:200] if effective_raw_text else None))
                payload = {
                    'status': 'completed',
                    'summary': summary_text or (f'Completed task via tools (synthesized after {guard_trip})' if guard_trip else 'Completed task via tools'),
                    'report_markdown': effective_raw_text or (f'Completed task via tools (synthesized after {guard_trip})' if guard_trip else 'Completed task via tools'),
                    'modified_files': written_files,
                    'blockers': [],
                }
        else:
            if guard_trip:
                decision_source = "model"
            payload.setdefault('status', 'completed')
            payload.setdefault('summary', 'Completed task via tools')
            payload.setdefault('report_markdown', payload.get('summary', 'Execution completed via governed tools.'))
            payload.setdefault('blockers', [])
            if not payload.get('modified_files') and written_files:
                payload['modified_files'] = written_files
            elif written_files:
                current_mods = {Path(p).as_posix().lstrip('/') for p in payload.get('modified_files', [])}
                for wf in written_files:
                    if Path(wf).as_posix().lstrip('/') not in current_mods:
                        payload.setdefault('modified_files', []).append(wf)

        # Prune internal scratch/decision paths and non-existent phantom files from modified_files
        is_read_only = (
            (kernel_contract is not None and getattr(kernel_contract, 'write_mode', None) == 'read-only')
            or is_integration_review
            or is_delivery_status
        )
        if is_read_only:
            payload['modified_files'] = []
        elif payload.get('modified_files'):
            pruned_mods: list[str] = []
            for p in payload['modified_files']:
                norm_p = str(p).replace('\\', '/').lstrip('/')
                if norm_p.startswith(('_scratch', 'scratch', '.sync/state', '.sync/runtime')):
                    continue
                if tool_runtime is not None:
                    is_written = norm_p in written_files or any(Path(wf).as_posix().lstrip('/') == norm_p for wf in written_files)
                    if not is_written:
                        continue
                pruned_mods.append(p)
            payload['modified_files'] = pruned_mods

        if request.task.work_order_id and payload.get('status') == 'completed' and not str(payload.get('release_target') or '').strip():
            payload['release_target'] = request.task.deliverable_path or 'patch'
        elif not payload.get('release_target') or str(payload.get('release_target')).strip().lower() in ('none', 'null', ''):
            payload.pop('release_target', None)

        allowed_keys = {
            'status',
            'summary',
            'report_markdown',
            'blockers',
            'blocker_details',
            'modified_files',
            'release_target',
            'retrieval_queries',
            'uncertainty',
            'commands',
        }
        payload = {k: v for k, v in payload.items() if k in allowed_keys}
        completion_meta: dict[str, Any] = {}
        if guard_trip:
            completion_meta['guard_trip'] = guard_trip
            completion_meta['decision_source'] = decision_source
        completion_meta['tool_calls_audit'] = tool_calls_audit
        if tool_runtime is not None and hasattr(tool_runtime, "gateway"):
            if getattr(tool_runtime.gateway, "triggered_goal", None):
                completion_meta['triggered_goal'] = tool_runtime.gateway.triggered_goal
            if getattr(tool_runtime.gateway, "delegated_tasks", None):
                completion_meta['delegated_tasks'] = tool_runtime.gateway.delegated_tasks

        return CompletionRecord(
            provider=getattr(self.provider_adapter, 'provider_name', self.backend_id),
            model=getattr(self.provider_adapter, 'model_name', self.backend_model),
            payload=payload,
            prompt_tokens=gateway.total_usage.prompt_tokens,
            completion_tokens=gateway.total_usage.completion_tokens,
            meta=completion_meta,
        )
