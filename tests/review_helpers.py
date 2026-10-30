"""Shared helpers for tests that must satisfy the mandatory review gates.

Kept deliberately tiny: tests provision a *structured, revision-bound* verdict
exactly the way the real QA/Architect reviewers do, so the gate logic under test
is never bypassed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from validators.harness import review_gate
from validators.kernel.io import durable_write_yaml


def qa_pass(ws: Path, wo_id: str, *, reviewer: str = "gemma") -> dict[str, Any]:
    """Persist a valid QA PASS verdict bound to the work order's current revision."""
    verdict = {
        "schema": review_gate.QA_VERDICT_SCHEMA,
        "work_order_id": wo_id,
        "revision": 1,
        "reviewer": reviewer,
        "reviewer_role": "qa",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "verdict": "PASS",
        "subject_hash": review_gate.subject_hash(ws, wo_id),
        "criteria": [
            {
                "id": "AC-1",
                "description": "declared acceptance criteria are satisfied",
                "outcome": "PASS",
                "evidence": "pytest -q",
            }
        ],
        "evidence": [
            {"kind": "test", "command": "pytest -q", "result": "passed", "passed": True}
        ],
        "findings": [],
        "required_actions": [],
        "rework_attempt": 0,
        "rework_budget": 2,
    }
    review_gate.persist_qa_verdict(ws, verdict)
    return verdict


def final_pass(
    ws: Path,
    run_id: str,
    plan_id: str,
    wo_ids: list[str],
    *,
    reviewer: str = "claude",
) -> dict[str, Any]:
    """Persist a valid Architect final-review PASS for the current revisions."""
    verdict = {
        "schema": review_gate.FINAL_REVIEW_SCHEMA,
        "run_id": run_id,
        "plan_id": plan_id,
        "plan_hash": review_gate.plan_hash(ws),
        "status": "PASS",
        "reviewer": reviewer,
        "reviewer_role": "architecture",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "reviewed_work_orders": [
            {"id": wo_id, "subject_hash": review_gate.subject_hash(ws, wo_id)}
            for wo_id in wo_ids
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "description": "plan delivered",
                "outcome": "PASS",
                "evidence": "pytest -q",
            }
        ],
        "evidence": [
            {"kind": "test", "command": "pytest -q", "result": "passed", "passed": True}
        ],
        "findings": [],
        "required_actions": [],
        "attempt": 1,
    }
    review_gate.persist_final_verdict(ws, verdict)
    if not (ws / "PLAN.md").is_file():
        (ws / "PLAN.md").write_text("# Approved Plan\n\nDeliver the goal.\n", encoding="utf-8")
        verdict["plan_hash"] = review_gate.plan_hash(ws)
        review_gate.persist_final_verdict(ws, verdict)
    return verdict


def ensure_completed(ws: Path, wo_id: str, *, to: str = "COMPLETED") -> None:
    """Move a work order YAML into the COMPLETED archive (test scaffolding)."""
    src = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
    dst_dir = ws / ".sync" / "work-orders" / to
    dst_dir.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        data["status"] = to
        durable_write_yaml(dst_dir / f"{wo_id}.yaml", data)
        src.unlink()
