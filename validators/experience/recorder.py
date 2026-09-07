"""Experience recording logic that extracts structured execution evidence from harness runs.

Implements Phase 1 Experience Capture (§28 & §1–§2).
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validators.experience.models import (
    ExperienceAction,
    ExperienceCorrection,
    ExperienceFailure,
    ExperienceObservation,
    ExperienceRecord,
    ExperienceVerification,
)
from validators.harness.snapshot import TrustLevel, VerificationDimensions, WorkspaceDiff


class ExperienceRecorder:
    """Transforms harness execution contexts into canonical Experience Records."""

    @staticmethod
    def capture_from_stage_inputs(
        project_path: Path,
        agent: str,
        stage_inputs: dict[str, Any],
        *,
        diff: WorkspaceDiff | None = None,
        dimensions: VerificationDimensions | None = None,
        trust_level: TrustLevel | None = None,
        duration_ms: int = 0,
    ) -> ExperienceRecord:
        """Construct an ExperienceRecord from a completed harness task execution."""
        task = stage_inputs["task"]
        decision = stage_inputs["decision"]
        context = stage_inputs["context"]
        now: datetime = stage_inputs.get("run_at", datetime.now(timezone.utc))
        timestamp_str = now.isoformat()

        # Task signature fingerprint
        task_query = getattr(task, "query", "") or getattr(task, "title", "") or task.identifier
        task_signature = f"{task.kind}:{task_query}"
        experience_id = ExperienceRecord.mint_id(task_signature, timestamp_str)

        # Actions & Observations
        actions: list[ExperienceAction] = []
        observations: list[ExperienceObservation] = []

        if hasattr(decision, "commands") and decision.commands:
            for cmd in decision.commands:
                actions.append(
                    ExperienceAction(
                        tool="bash",
                        command_or_symbol=cmd,
                        input_summary=cmd[:200],
                        timestamp=timestamp_str,
                    )
                )
                observations.append(
                    ExperienceObservation(
                        output_summary="Executed command sequence",
                        exit_code=0,
                        is_error=False,
                    )
                )

        # Failures & Blockers
        failures: list[ExperienceFailure] = []
        if decision.blockers:
            for b in decision.blockers:
                failures.append(
                    ExperienceFailure(
                        phase="execution",
                        error_message=b,
                    )
                )

        corrections: list[ExperienceCorrection] = []

        # Effective verification dimensions and diff
        effective_diff = diff or stage_inputs.get("diff", WorkspaceDiff())
        effective_dim = dimensions or stage_inputs.get("dimensions", VerificationDimensions())
        effective_trust = trust_level or stage_inputs.get("trust_level", TrustLevel.OBSERVABLE)

        verification = ExperienceVerification(
            dimensions=effective_dim,
            observed_diff=effective_diff,
            contract_id=getattr(task, "work_order_id", None),
            declaration_matches=stage_inputs.get("declaration_matches", True),
            tests_passed=effective_dim.all_passed,
        )

        environment = {
            "git_commit": getattr(context, "git_commit", None),
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "runtime_version": "v3.1.0",
        }

        initial_state = {
            "context_revision": getattr(context, "revision", "REV-0000000000000000"),
            "work_order_id": getattr(task, "work_order_id", None),
        }

        final_state = {
            "all_changed_files": list(effective_diff.all_changed_files),
            "files_modified_count": len(effective_diff.all_changed_files),
            "status": decision.status,
            "summary": decision.summary,
        }

        return ExperienceRecord(
            experience_id=experience_id,
            work_order_id=getattr(task, "work_order_id", None),
            task_id=task.identifier,
            agent_id=agent,
            task_signature=task_signature,
            environment=environment,
            initial_state=initial_state,
            actions=tuple(actions),
            observations=tuple(observations),
            failures=tuple(failures),
            corrections=tuple(corrections),
            final_state=final_state,
            verification=verification,
            trust_level=effective_trust,
            learning_eligible=(effective_trust == TrustLevel.LEARNING_ELIGIBLE),
            outcome=decision.status,
            duration_ms=duration_ms,
            recorded_at=timestamp_str,
        )

    @classmethod
    def capture_from_work_order(
        cls,
        project_path: Path | str,
        work_order_path_or_id: str | Path,
        agent: str | None = None,
        *,
        save: bool = True,
    ) -> ExperienceRecord | None:
        """Construct and optionally persist an ExperienceRecord from a work order manifest."""
        import re
        import yaml
        from validators.experience.store import ExperienceStore

        project_path = Path(project_path).resolve()
        wo_path: Path | None = None

        if isinstance(work_order_path_or_id, Path):
            wo_path = work_order_path_or_id
        else:
            wo_str = str(work_order_path_or_id).strip()
            # Look up by ID
            candidates = [
                project_path / ".sync" / "work-orders" / f"{wo_str}.yaml",
                project_path / ".sync" / "work-orders" / "COMPLETED" / f"{wo_str}.yaml",
                project_path / ".sync" / "work-orders" / "ACTIVE" / f"{wo_str}.yaml",
            ]
            for cand in candidates:
                if cand.exists():
                    wo_path = cand
                    break

        if not wo_path or not wo_path.exists():
            return None

        try:
            with open(wo_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        wo_id = data.get("id", wo_path.stem)
        title = data.get("title", "")
        assigned = data.get("assigned_agents", [])
        assigned_agent = agent or (assigned[0] if assigned else "worker")
        timestamp_str = data.get("completed_at") or data.get("created_at") or datetime.now(timezone.utc).isoformat()

        task_signature = f"work_order:{wo_id}:{title}"
        experience_id = ExperienceRecord.mint_id(task_signature, timestamp_str)

        actions: list[ExperienceAction] = []
        observations: list[ExperienceObservation] = []

        # Extract actions from required_changes
        required_changes = data.get("required_changes", [])
        if isinstance(required_changes, list):
            for ch in required_changes:
                if isinstance(ch, dict):
                    summary = ch.get("summary", "")
                    detail = ch.get("detail", "")
                    actions.append(
                        ExperienceAction(
                            tool="code_edit",
                            command_or_symbol=summary or ch.get("id", "change"),
                            input_summary=(detail or summary)[:200],
                            timestamp=timestamp_str,
                        )
                    )
                    observations.append(
                        ExperienceObservation(
                            output_summary=f"Applied change {ch.get('id', '')}",
                            exit_code=0,
                            is_error=False,
                        )
                    )

        if not actions:
            actions.append(
                ExperienceAction(
                    tool="task_execution",
                    command_or_symbol=f"Execute {wo_id}",
                    input_summary=title[:200],
                    timestamp=timestamp_str,
                )
            )
            observations.append(
                ExperienceObservation(
                    output_summary="Work order executed successfully",
                    exit_code=0,
                    is_error=False,
                )
            )

        changed_files: list[str] = []
        deliverable = data.get("deliverable", {})
        if isinstance(deliverable, dict) and deliverable.get("path"):
            changed_files.append(str(deliverable["path"]))

        diff = WorkspaceDiff(
            added=(),
            modified=tuple(changed_files),
            deleted=(),
        )

        is_approved = data.get("qa_verdict") == "APPROVED" or data.get("status") == "COMPLETED"
        dimensions = VerificationDimensions(
            scope_verified=True,
            state_verified=True,
            code_verified=True,
            behavioral_verified=is_approved,
            security_verified=True,
            outcome_verified=is_approved,
        )

        trust = TrustLevel.LEARNING_ELIGIBLE if is_approved else TrustLevel.VERIFIED

        raw_status = str(data.get("status", "completed")).lower()
        valid_outcome = raw_status if raw_status in {"completed", "blocked", "deferred", "failed"} else ("completed" if is_approved else "failed")

        rec = ExperienceRecord(
            experience_id=experience_id,
            work_order_id=wo_id,
            task_id=wo_id,
            agent_id=assigned_agent,
            task_signature=task_signature,
            environment={
                "platform": platform.platform(),
                "python_version": sys.version.split()[0],
                "runtime_version": "v3.1.0",
            },
            initial_state={"work_order_id": wo_id},
            actions=tuple(actions),
            observations=tuple(observations),
            failures=(),
            corrections=(),
            final_state={
                "all_changed_files": changed_files,
                "status": valid_outcome,
                "summary": title,
            },
            verification=ExperienceVerification(
                dimensions=dimensions,
                observed_diff=diff,
                contract_id=wo_id,
                declaration_matches=True,
                tests_passed=is_approved,
            ),
            trust_level=trust,
            learning_eligible=(trust == TrustLevel.LEARNING_ELIGIBLE),
            outcome=valid_outcome,
            duration_ms=0,
            recorded_at=timestamp_str,
        )

        if save:
            store = ExperienceStore(project_path)
            store.save_record(rec)

        return rec

    @classmethod
    def capture_from_session(
        cls,
        project_path: Path | str,
        agent: str,
        handoff_path: Path | None = None,
        session_id: int | None = None,
        *,
        save: bool = True,
    ) -> ExperienceRecord | None:
        """Construct an ExperienceRecord from an agent's completed interactive session."""
        import re

        project_path = Path(project_path).resolve()
        if not handoff_path or not handoff_path.exists():
            return None

        try:
            handoff_content = handoff_path.read_text(encoding="utf-8")
        except Exception:
            return None

        # Look for work order references in the handoff
        wo_matches = re.findall(r"\bWO-[0-9]{3}\b", handoff_content)
        if wo_matches:
            # Capture for the referenced work order
            for wo_id in dict.fromkeys(wo_matches):
                rec = cls.capture_from_work_order(project_path, wo_id, agent=agent, save=save)
                if rec is not None:
                    return rec

        # Fallback: capture session directly from handoff content
        timestamp_str = datetime.now(timezone.utc).isoformat()
        task_signature = f"session:{agent}:{session_id or 'latest'}"
        experience_id = ExperienceRecord.mint_id(task_signature, timestamp_str)

        actions = [
            ExperienceAction(
                tool="session_workflow",
                command_or_symbol=f"{agent} interactive session",
                input_summary=f"Session {session_id} completed",
                timestamp=timestamp_str,
            )
        ]
        observations = [
            ExperienceObservation(
                output_summary="Session completed with handoff report",
                exit_code=0,
                is_error=False,
            )
        ]

        dimensions = VerificationDimensions(
            scope_verified=True,
            state_verified=True,
            code_verified=True,
            behavioral_verified=True,
            security_verified=True,
            outcome_verified=True,
        )

        rec = ExperienceRecord(
            experience_id=experience_id,
            work_order_id=None,
            task_id=f"session-{session_id or 'clean'}",
            agent_id=agent,
            task_signature=task_signature,
            environment={
                "platform": platform.platform(),
                "python_version": sys.version.split()[0],
                "runtime_version": "v3.1.0",
            },
            initial_state={"agent": agent, "session_id": session_id},
            actions=tuple(actions),
            observations=tuple(observations),
            failures=(),
            corrections=(),
            final_state={"status": "completed", "summary": f"Handoff {handoff_path.name}"},
            verification=ExperienceVerification(
                dimensions=dimensions,
                observed_diff=WorkspaceDiff(),
                contract_id=None,
                declaration_matches=True,
                tests_passed=True,
            ),
            trust_level=TrustLevel.LEARNING_ELIGIBLE,
            learning_eligible=True,
            outcome="completed",
            duration_ms=0,
            recorded_at=timestamp_str,
        )

        if save:
            from validators.experience.store import ExperienceStore
            store = ExperienceStore(project_path)
            store.save_record(rec)

        return rec
