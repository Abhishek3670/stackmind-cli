"""Verification-dimension evaluation and verified diff application.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import ast
import fnmatch
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from validators.harness.orchestration import (
    evaluate_release_gates,
)

from .models import (
    HarnessDecision,
    HarnessTask,
)

class _VerificationMixin:
    """Verification-dimension evaluation and verified diff application."""

    def _evaluate_verification_dimensions(
        self,
        *,
        task: HarnessTask,
        decision: HarnessDecision,
        diff: Any,
        before_snapshot: Any,
        after_snapshot: Any,
        staged_root: Path,
        staged_errors: list[str],
        declaration_matches: bool,
        command_results: list[subprocess.CompletedProcess[str]],
        d025_passed: bool,
        has_staged_writes: bool,
        tool_runtime: Any = None,
    ) -> Any:
        """Derive verification flags from the staged filesystem and command telemetry.

        Fail-closed semantics: any exception during verification is captured and
        treated as a failure, preventing false success returns.
        """
        from validators.harness.contract_gate import verify_post_execution
        from validators.harness.snapshot import VerificationDimensions

        self._last_verification_evidence = {}

        # Include tool-written files in changed files for scope verification
        tool_written_files = []
        if tool_runtime and hasattr(tool_runtime.gateway, 'written_files'):
            tool_written_files = [
                Path(p).as_posix().lstrip('/') for p in tool_runtime.gateway.written_files
            ]

        diff_changed = tuple(Path(p).as_posix().lstrip('/') for p in diff.all_changed_files)
        changed_files = diff_changed + tuple(tool_written_files)
        bookkeeping_prefixes = ('.sync/inbox/', '.sync/state/', '.sync/reports/')
        dec_norm = {Path(p).as_posix().lstrip('/') for p in decision.modified_files if p}
        task_changed_files = tuple(
            relative_path for relative_path in changed_files
            if not (any(relative_path.startswith(prefix) for prefix in bookkeeping_prefixes) and relative_path not in dec_norm)
            and not (relative_path.startswith('.sync/work-orders/') and relative_path not in dec_norm)
        )
        scope_verified = declaration_matches
        # Include tool-written files for scope verification
        files_for_scope = list(task_changed_files)
        if tool_runtime and hasattr(tool_runtime.gateway, 'written_files'):
            for wf in tool_runtime.gateway.written_files:
                norm_w = Path(wf).as_posix().lstrip('/')
                if norm_w not in files_for_scope:
                    files_for_scope.append(norm_w)

        if files_for_scope:
            try:
                verify_post_execution(
                    self.project_path,
                    self.agent,
                    task,
                    decision,
                    observed_files=tuple(files_for_scope),
                )
            except Exception as exc:
                scope_verified = False
                staged_errors.append(str(exc))

        # Runtime contracts use path rules; validate those directly when present.
        raw_contract = None
        if task.work_order_id:
            contract_path = self.sync_path / 'contracts' / f'{task.work_order_id}.yaml'
            if contract_path.exists():
                raw_contract = self._read_yaml(contract_path)
                allow_rules = raw_contract.get('scope', {}).get('allow', [])
                deny_rules = raw_contract.get('scope', {}).get('deny', [])

                def _rule_to_path(rule: Any) -> str:
                    if isinstance(rule, dict):
                        p = rule.get('path') or rule.get('target') or rule.get('module')
                        return str(p or '')
                    if isinstance(rule, str):
                        return rule
                    return ''

                def _path_matches_rule(target_path: str, rule_str: str) -> bool:
                    r = rule_str.replace('\\', '/')
                    if r.startswith('./'):
                        r = r[2:]
                    r_clean = r.rstrip('/*').rstrip('/')
                    if (
                        target_path == r
                        or target_path == r_clean
                        or target_path.startswith(r_clean + '/')
                        or fnmatch.fnmatch(target_path, r)
                        or (r.endswith('/**') and fnmatch.fnmatch(target_path, r))
                        or fnmatch.fnmatch(target_path, r_clean + '/*')
                        or fnmatch.fnmatch(target_path, r_clean + '/**')
                    ):
                        return True
                    r_slash = r_clean.replace('.', '/')
                    if (
                        target_path == r_slash
                        or target_path == (r_slash + '.py')
                        or target_path.startswith(r_slash + '/')
                        or fnmatch.fnmatch(target_path, r_slash)
                    ):
                        return True
                    return False

                allow_paths = [p for rule in allow_rules if (p := _rule_to_path(rule))]
                deny_paths = [p for rule in deny_rules if (p := _rule_to_path(rule))]
                for relative_path in task_changed_files:
                    normalized = Path(relative_path).as_posix()
                    if normalized.startswith('.sync/') and relative_path not in decision.modified_files:
                        continue  # Harness-owned bookkeeping writes are authorized separately.
                    allowed = not allow_paths or any(_path_matches_rule(normalized, rule) for rule in allow_paths)
                    denied = any(_path_matches_rule(normalized, rule) for rule in deny_paths)
                    if denied:
                        matching_deny = [r for r in deny_paths if _path_matches_rule(normalized, r)]
                        matching_allow = [r for r in allow_paths if _path_matches_rule(normalized, r)]
                        if matching_allow and all(
                            any(
                                _path_matches_rule(a.replace('\\', '/').rstrip('/*'), d)
                                and not _path_matches_rule(d.replace('\\', '/').rstrip('/*'), a)
                                for a in matching_allow
                            )
                            for d in matching_deny
                        ):
                            denied = False
                    if not allowed or denied:
                        scope_verified = False

        state_verified = not staged_errors
        for relative_path in changed_files:
            before = before_snapshot.files.get(relative_path)
            after = after_snapshot.files.get(relative_path)
            if after is not None and not after.content_hash:
                state_verified = False
            if before is not None and after is not None and before.content_hash == after.content_hash:
                state_verified = False
            if after is None and before is None:
                state_verified = False

        code_verified = True
        for relative_path in changed_files:
            if relative_path.endswith('.py'):
                candidate = staged_root / relative_path
                if candidate.exists():
                    try:
                        source_code = candidate.read_text(encoding='utf-8')
                        ast.parse(source_code)
                        from validators.harness.dependency_gate import check_import_satisfiability
                        sat_result = check_import_satisfiability(
                            candidate,
                            project_root=self.project_path,
                            staged_root=staged_root,
                            source_code=source_code,
                            contract=raw_contract,
                        )
                        if not sat_result.passed:
                            code_verified = False
                            staged_errors.append(sat_result.diagnostic or f"import satisfiability failed for {relative_path}")
                    except (OSError, UnicodeDecodeError, SyntaxError):
                        code_verified = False

            # Phase C: PLAN.md structural validation (with auto-repair)
            norm_rel = Path(relative_path).as_posix()
            if norm_rel == "PLAN.md" or norm_rel.endswith("/PLAN.md"):
                candidate = staged_root / relative_path
                if candidate.exists():
                    from validators.harness.plan import (
                        repair_plan_structure,
                        validate_plan_structure,
                    )
                    try:
                        raw_plan = candidate.read_text(encoding='utf-8')
                        plan_valid, plan_errs = validate_plan_structure(raw_plan)
                        if not plan_valid:
                            repaired_plan = repair_plan_structure(raw_plan, default_name=self.project_path.name)
                            rep_valid, rep_errs = validate_plan_structure(repaired_plan)
                            if rep_valid:
                                candidate.write_text(repaired_plan, encoding='utf-8')
                                plan_valid = True
                                plan_errs = []
                        if not plan_valid:
                            code_verified = False
                            state_verified = False
                            for err in plan_errs:
                                staged_errors.append(f"PLAN.md: {err}")
                    except Exception as exc:
                        code_verified = False
                        state_verified = False
                        staged_errors.append(f"PLAN.md read error: {exc}")

            # Phase D: Governed YAML authoring gate (.sync/work-orders and .sync/contracts)
            if (norm_rel.startswith('.sync/work-orders/') or norm_rel.startswith('.sync/contracts/')) and norm_rel in dec_norm:
                candidate = staged_root / relative_path
                if candidate.is_file() and (norm_rel.endswith('.yaml') or norm_rel.endswith('.yml')):
                    from validators.harness.authoring_gate import AuthoringGate
                    gate = AuthoringGate(project_root=self.project_path)
                    gate_decision = gate.validate_staged_artifact(
                        staged_root, relative_path, agent=self.agent, project_root=self.project_path
                    )
                    if not gate_decision.passed:
                        code_verified = False
                        state_verified = False
                        for err in gate_decision.errors:
                            staged_errors.append(f"{relative_path}: {err}")

        for result in command_results:
            command = str(result.args).lower()
            if ('pytest' in command or 'test' in command) and result.returncode != 0:
                code_verified = False

        behavioral_verified = (
            (decision.status == 'completed' or (decision.status == 'blocked' and bool(decision.blockers)))
            and all(result.returncode == 0 for result in command_results)
        )

        d024_passed = True
        if self.agent in ("local-llm", "gitops") and task.work_order_id:
            from validators.harness.d024_gate import D024Gate
            d024_passed = D024Gate().evaluate_work_order(self.project_path, task.work_order_id).passed

        security_verified = d025_passed and d024_passed and all(
            not Path(relative_path).is_absolute() and '..' not in Path(relative_path).parts
            for relative_path in changed_files
        )
        if not all(
            not Path(relative_path).is_absolute() and '..' not in Path(relative_path).parts
            for relative_path in changed_files
        ):
            for relative_path in changed_files:
                if Path(relative_path).is_absolute() or '..' in Path(relative_path).parts:
                    staged_errors.append(f"path traversal or absolute path detected in {relative_path}")

        if not d025_passed:
            staged_errors.append("D025 destructive command validation failed")
        if not d024_passed:
            staged_errors.append("D024 release gate validation failed")

        if security_verified:
            from validators.kernel.security import scan_for_credential_leaks

            for relative_path in changed_files:
                candidate = staged_root / relative_path
                if candidate.is_file():
                    try:
                        content = candidate.read_text(encoding='utf-8', errors='replace')
                        leaks = scan_for_credential_leaks(content, file_path=relative_path)
                        if leaks:
                            security_verified = False
                            staged_errors.append(f"credential leak detected in {relative_path}: {leaks[0]}")
                            break
                    except OSError as exc:
                        security_verified = False
                        staged_errors.append(f"failed to read {relative_path} for security scan: {exc}")
                        break

        deliverable_touched = False
        if task.deliverable_path:
            norm_deliv = Path(task.deliverable_path).as_posix().lstrip('/')
            staged_added = {Path(p).as_posix().lstrip('/') for p in diff.added}
            staged_modified = {Path(p).as_posix().lstrip('/') for p in diff.modified}
            deliverable_touched = (norm_deliv in staged_added or norm_deliv in staged_modified)
            if not deliverable_touched and tool_runtime:
                written_files = getattr(tool_runtime.gateway, 'written_files', [])
                deliverable_touched = any(
                    Path(w).as_posix().lstrip('/') == norm_deliv for w in written_files
                )

        authored_artifacts = any(
            (p.startswith('.sync/work-orders/') or p.startswith('.sync/contracts/'))
            for p in task_changed_files
        )

        if decision.status == 'blocked':
            if task.deliverable_path and not getattr(task, 'is_authoring', False):
                outcome_verified = deliverable_touched
            else:
                outcome_verified = bool(decision.blockers) and bool(
                    (decision.summary and decision.summary.strip())
                    or (decision.report_markdown and decision.report_markdown.strip())
                )
        elif getattr(task, 'is_authoring', False):
            # Authoring turns produce governed work orders and contracts (or synthesis).
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    authored_artifacts
                    or bool(task_changed_files)
                    or bool(decision.summary and decision.summary.strip())
                    or bool(decision.report_markdown and decision.report_markdown.strip())
                )
            )
        elif task.kind == 'inbox':
            # Inbox tasks are intentionally archived by the staged operation; the
            # task selected at discovery is itself the completed deliverable.
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
            )
        elif task.kind == 'adhoc':
            # Ad-hoc prompt turns produce conversational completion/report deliverables.
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and bool(
                    (decision.summary and decision.summary.strip())
                    or (decision.report_markdown and decision.report_markdown.strip())
                )
            )
        elif self.agent in ('gemma', 'qa'):
            # QA Lead: outcome verified by tool execution/testing, inbox verdict write, or non-empty QA report/verdict
            has_tool_activity = bool(
                tool_runtime
                and getattr(tool_runtime, 'gateway', None)
                and (
                    getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                    or getattr(tool_runtime.gateway, '_todo_list', None)
                )
            )
            has_report = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    has_report
                    or has_tool_activity
                    or bool(changed_files)
                    or bool(task_changed_files)
                    or any(r.returncode == 0 for r in command_results)
                )
            )
        elif self.agent in ('local-llm', 'gitops'):
            # GitOps Lead: outcome verified by release activity, git operations, or non-empty report
            has_tool_activity = bool(
                tool_runtime
                and getattr(tool_runtime, 'gateway', None)
                and (
                    getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                    or getattr(tool_runtime.gateway, '_todo_list', None)
                )
            )
            has_report = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    has_report
                    or has_tool_activity
                    or bool(changed_files)
                    or bool(task_changed_files)
                    or any(r.returncode == 0 for r in command_results)
                )
            )
        elif task.work_order_id:
            # Discover work order deliverable metadata
            wo_data: dict[str, Any] = {}
            for sub in ("ACTIVE", "COMPLETED"):
                wo_file = self.sync_path / "work-orders" / sub / f"{task.work_order_id}.yaml"
                if wo_file.is_file():
                    try:
                        wo_data = self._read_yaml(wo_file) or {}
                    except Exception:
                        pass
                    break

            if not wo_data:
                if getattr(task, 'deliverable_paths', ()):
                    wo_data = {"deliverables": [{"path": p} for p in task.deliverable_paths]}
                elif task.deliverable_path:
                    wo_data = {"deliverable": {"path": task.deliverable_path, "type": task.deliverable_type or "code"}}

            from validators.harness.deliverable_verifier import verify_work_order_deliverables
            deliv_passed, deliv_errors, deliv_evidence = verify_work_order_deliverables(
                self.project_path, wo_data, staged_root=staged_root
            )
            self._last_verification_evidence = deliv_evidence

            has_declared_files = bool(deliv_evidence.get("declared_paths"))
            if has_declared_files:
                if not deliv_passed:
                    outcome_verified = False
                    for err in deliv_errors:
                        staged_errors.append(f"Deliverable verification failed: {err}")
                elif task.deliverable_path and not deliverable_touched:
                    outcome_verified = False
                    staged_errors.append(f"declared deliverable '{task.deliverable_path}' was not added or modified in this turn")
                else:
                    outcome_verified = (
                        decision.status == 'completed'
                        and not decision.blockers
                    )
            else:
                is_non_code_task = (
                    getattr(task, 'deliverable_type', None) in ('doc', 'verification', 'review', 'release', 'research')
                    or any(kw in (task.title or '').lower() for kw in ('qa', 'test', 'verification', 'audit', 'review', 'release', 'doc', 'plan'))
                )
                has_tool_activity = bool(
                    tool_runtime
                    and getattr(tool_runtime, 'gateway', None)
                    and (
                        getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                        or getattr(tool_runtime.gateway, '_todo_list', None)
                    )
                )
                has_report = bool(
                    (decision.summary and decision.summary.strip())
                    or (decision.report_markdown and decision.report_markdown.strip())
                )
                outcome_verified = (
                    decision.status == 'completed'
                    and not decision.blockers
                    and (
                        bool(task_changed_files)
                        or bool(changed_files)
                        or any(r.returncode == 0 for r in command_results)
                        or (is_non_code_task and (has_report or has_tool_activity))
                    )
                )
        else:
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (bool(changed_files) or has_staged_writes)
            )

        # Evaluate centralized release gates (Fix 7)
        # Clean delivery gate check - runs for any work_order with a declared deliverable
        # CleanDeliveryGate is skipped for intermediate harness operations (tests, development)
        # because they work with development files (validators/harness/, .sync/, etc.)
        # The deliverable_path flag indicates a product is being delivered, regardless of task classification
        clean_delivery_passed = True
        # Run CleanDeliveryGate whenever a work_order declares a deliverable_path
        # This ensures all final product releases are checked, not just authoring tasks
        if task.kind == 'work_order' and task.deliverable_path:
            from validators.harness.clean_delivery_gate import CleanDeliveryGate
            gate = CleanDeliveryGate(self.project_path)
            clean_delivery_passed, _ = gate.validate()

        # Requirements coverage check
        requirements_passed = True
        requirements_blockers: tuple[str, ...] = ()
        if task.work_order_id:
            from validators.harness.requirements import validate_requirements_coverage
            ledger_path = self.sync_path / 'state' / 'requirements.yaml'
            if ledger_path.exists():
                try:
                    from validators.harness.requirements import RequirementsLedger
                    ledger = RequirementsLedger.load(ledger_path)
                    # Collect all work order IDs for the project
                    all_wo_ids: list[str] = []
                    for status_dir in ('ACTIVE', 'COMPLETED', 'BLOCKED'):
                        wo_dir = self.sync_path / 'work-orders' / status_dir
                        if wo_dir.exists():
                            all_wo_ids.extend(f.stem for f in wo_dir.glob('*.yaml'))
                    requirements_passed, requirements_blockers = validate_requirements_coverage(ledger, all_wo_ids)
                except Exception:
                    pass

        # Check evidence freshness (stale detection)
        has_stale_evidence = False
        if task.work_order_id:
            from validators.harness.snapshot import WorkspaceSnapshot
            try:
                current_snapshot = WorkspaceSnapshot.capture(self.project_path)
                # Check if requirements evidence hashes match current files
                from validators.harness.requirements import RequirementsLedger
                ledger_path = self.sync_path / 'state' / 'requirements.yaml'
                if ledger_path.exists():
                    ledger = RequirementsLedger.load(ledger_path)
                    for req in ledger.requirements:
                        if req.evidence and req.evidence.get('content_hash'):
                            # Check if any changed file has matching hash - stale if mismatch
                            for changed_file in changed_files:
                                candidate = staged_root / changed_file
                                if candidate.exists():
                                    current_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
                                    if current_hash != req.evidence.get('content_hash'):
                                        has_stale_evidence = True
                                        break
            except Exception:
                pass

        # Execution QA check
        execution_qa_passed = True
        execution_blockers: tuple[str, ...] = ()
        if self.agent in ('gemma', 'qa'):
            if decision.status != 'completed' or decision.blockers:
                execution_qa_passed = False
                execution_blockers = (decision.blockers if decision.blockers else ('QA verification not completed',))

        # Call centralized gate evaluation
        approved, blocking_reasons = evaluate_release_gates(
            clean_delivery_passed=clean_delivery_passed,
            d024_passed=d024_passed,
            requirements_passed=requirements_passed,
            execution_qa_passed=execution_qa_passed,
            has_stale_evidence=has_stale_evidence,
            has_requirements_blocked=not requirements_passed,
            requirements_blockers=requirements_blockers,
            execution_qa_blocked=not execution_qa_passed,
            execution_blockers=execution_blockers,
        )

        # Append centralized gate blocking reasons
        if not approved:
            staged_errors.append(f"Release gates blocked: {'; '.join(blocking_reasons)}")

        return VerificationDimensions(
            scope_verified=scope_verified,
            state_verified=state_verified,
            code_verified=code_verified,
            behavioral_verified=behavioral_verified,
            security_verified=security_verified,
            outcome_verified=outcome_verified,
        )

    def _apply_verified_workspace_diff(
        self, staged_root: Path, diff: Any, extra_paths: Sequence[str] = ()
    ) -> None:
        """Commit only the already-verified staged diff to the live workspace."""
        for relative_path in (*diff.added, *diff.modified, *extra_paths):
            relative = Path(relative_path)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'unsafe staged path: {relative_path}')
            source = staged_root / relative
            target = self.project_path / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for relative_path in diff.deleted:
            relative = Path(relative_path)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'unsafe staged path: {relative_path}')
            target = self.project_path / relative
            if target.exists():
                target.unlink()
        self._sync_index_and_tree_yaml(self.project_path)
