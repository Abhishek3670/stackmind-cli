"""Unit tests for the three runtime hardening fixes:
1. Resilient QA verdict synthesis (handling pseudo-tool text and alternative verdict files).
2. Planning deliverable (PLAN.md) materialization and non-empty enforcement.
3. Role-model diversity checking.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, RunState
from validators.kernel.daemon.supervisor.release import completion_record_for
from validators.kernel.settings import check_role_diversity
from validators.kernel.providers.adapter import OpenAICompatibleAdapter
from validators.harness import review_gate


class _MockManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.plans: list[dict[str, Any]] = []

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        return self.plans

    def start_turn(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"operation_id": "op-test"}

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return {"operation_id": operation_id, "status": "COMPLETED", "result": {}}


# ─── Fix 1: Resilient QA Verdict Synthesis ────────────────────────────

def test_qa_verdict_synthesis_from_plain_text(tmp_path: Path) -> None:
    sup = LifecycleSupervisor(_MockManager(tmp_path))
    state = sup.start_run("run-test-qa", "Goal", tmp_path, "sess-test-qa")
    state.qa_review_attempts["WO-001"] = 0

    # Simulate an LLM operation that outputted pseudo-tool plain text
    op = {
        "operation_id": "op-qa-1",
        "agent_id": "gemma",
        "result": {
            "summary": "[Tool Use: write_file] Evaluated index.html and found no syntax errors. Verdict: NO_TEST_EVIDENCE",
        },
    }

    sup._synthesize_authoritative_qa_verdict(state, tmp_path, "WO-001", op)
    verdict = review_gate.load_qa_verdict(tmp_path, "WO-001")
    assert verdict is not None
    assert verdict["verdict"] == "NO_TEST_EVIDENCE"
    assert verdict["work_order_id"] == "WO-001"


def test_qa_verdict_synthesis_from_unfenced_json(tmp_path: Path) -> None:
    sup = LifecycleSupervisor(_MockManager(tmp_path))
    state = sup.start_run("run-test-qa-json", "Goal", tmp_path, "sess-test-qa-json")

    op = {
        "operation_id": "op-qa-2",
        "agent_id": "gemma",
        "result": {
            "summary": 'Here is the verdict: {"verdict": "PASS", "findings": []}',
        },
    }

    sup._synthesize_authoritative_qa_verdict(state, tmp_path, "WO-001", op)
    verdict = review_gate.load_qa_verdict(tmp_path, "WO-001")
    assert verdict is not None
    assert verdict["verdict"] == "PASS"


def test_qa_verdict_synthesis_from_alternative_disk_path(tmp_path: Path) -> None:
    sup = LifecycleSupervisor(_MockManager(tmp_path))
    state = sup.start_run("run-test-qa-alt", "Goal", tmp_path, "sess-test-qa-alt")

    # Write a verdict to alternative path .sync/qa/WO-001-review.yaml
    qa_dir = tmp_path / ".sync" / "qa"
    qa_dir.mkdir(parents=True)
    (qa_dir / "WO-001-review.yaml").write_text(
        "verdict: NO_TEST_EVIDENCE\nreviewer: gemma\nsummary: All deliverables verified.\n",
        encoding="utf-8",
    )

    sup._synthesize_authoritative_qa_verdict(state, tmp_path, "WO-001", None)
    canonical = review_gate.load_qa_verdict(tmp_path, "WO-001")
    assert canonical is not None
    assert canonical["verdict"] == "NO_TEST_EVIDENCE"


def test_openai_compatible_adapter_extracts_bracketed_tool_use() -> None:
    adapter = OpenAICompatibleAdapter(provider_name="custom", model="test-model")
    content = 'I will write the file.\n[Tool Use: write_file {"path": "index.html", "content": "<h1>Hello</h1>"}]\nDone.'

    extracted = adapter._extract_tool_calls_from_content(content)
    assert len(extracted) == 1
    assert extracted[0].name == "write_file"
    assert extracted[0].arguments["path"] == "index.html"


# ─── Fix 2: PLAN.md Materialization & Non-Empty Deliverables ──────────

def test_advance_planning_materializes_plan_md(tmp_path: Path) -> None:
    mgr = _MockManager(tmp_path)
    mgr.plans = [{
        "plan_id": "PLAN-001",
        "state": "APPROVED",
        "content": "# Test Architecture Plan\n\n## Milestones\n1. Milestone 1",
        "metadata": {"operation_id": "op-plan-1"},
    }]
    sup = LifecycleSupervisor(mgr)
    state = sup.start_run("run-test-plan", "Deliver product", tmp_path, "sess-test-plan")
    state.phase = Phase.PLANNING
    state.planning_operation_id = "op-plan-1"

    sup._advance_planning(state)

    plan_path = tmp_path / ".sync" / "playbook" / "PLAN.md"
    assert plan_path.is_file()
    assert "# Test Architecture Plan" in plan_path.read_text(encoding="utf-8")
    assert "WO-000" in state.completed_wo_ids


def test_completion_record_tracks_missing_deliverables(tmp_path: Path) -> None:
    wo_data = {
        "id": "WO-000",
        "deliverable": {"path": ".sync/playbook/PLAN.md", "type": "doc"},
    }
    # File does not exist yet on disk
    record = completion_record_for(tmp_path, "WO-000", wo_data, "2026-11-05T00:00:00Z")
    assert record["deliverable_hashes"] == {}
    assert ".sync/playbook/PLAN.md" in record.get("missing_deliverables", [])


# ─── Fix 3: Role-Model Diversity Check ────────────────────────────────

def test_check_role_diversity_flags_identical_qa_and_builder() -> None:
    roles_compromised = {
        "architecture": {"provider": "custom", "model": "inception/mercury-3"},
        "backend": {"provider": "custom", "model": "inception/mercury-3"},
        "frontend": {"provider": "custom", "model": "inception/mercury-3"},
        "qa": {"provider": "custom", "model": "inception/mercury-3"},
    }
    warnings = check_role_diversity(roles_compromised)
    assert len(warnings) >= 2
    assert any("grading its own work" in w for w in warnings)


def test_check_role_diversity_passes_when_decoupled() -> None:
    roles_diverse = {
        "architecture": {"provider": "anthropic", "model": "claude-3-7-sonnet-20250219"},
        "backend": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
        "frontend": {"provider": "gemini", "model": "gemini-2.0-flash"},
        "qa": {"provider": "ollama", "model": "gemma4:31b-cloud"},
    }
    warnings = check_role_diversity(roles_diverse)
    assert len(warnings) == 0
