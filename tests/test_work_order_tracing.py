"""Unit and integration tests for Work Order Tracing & Run Cost Ledger (Item C.6)."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest

from validators.kernel.tracing import (
    DEFAULT_AGENT_PRICING,
    WorkOrderTracer,
    compute_token_cost,
    record_operation_span,
)


def test_compute_token_cost():
    """Token cost computes accurately according to agent pricing tables."""
    # Claude: prompt $0.003/1k, completion $0.015/1k
    cost_claude = compute_token_cost("claude", prompt_tokens=1000, completion_tokens=1000)
    assert cost_claude == 0.018

    # Codex: prompt $0.0015/1k, completion $0.002/1k
    cost_codex = compute_token_cost("codex", prompt_tokens=2000, completion_tokens=1000)
    assert cost_codex == round(2 * 0.0015 + 1 * 0.002, 6)

    # Local-llm: zero cost
    cost_local = compute_token_cost("local-llm", prompt_tokens=5000, completion_tokens=5000)
    assert cost_local == 0.0


def test_work_order_tracer_records_spans_and_aggregates(tmp_path: Path):
    """Tracer persists spans to .sync/traces/{wo_id}.json and aggregates metrics."""
    tracer = WorkOrderTracer(tmp_path)

    # 1. Record Claude turn (planning/authoring)
    t1 = tracer.record_span(
        work_order_id="WO-001",
        span_id="op-001",
        agent_id="claude",
        role="architecture",
        action="author_work_order",
        prompt_tokens=1000,
        completion_tokens=500,
        run_id="run-test-1",
    )
    assert t1["total_tokens"] == 1500
    assert t1["cost_by_agent"]["claude"] > 0
    assert len(t1["spans"]) == 1

    # 2. Record Codex turn (worker implementation)
    t2 = tracer.record_span(
        work_order_id="WO-001",
        span_id="op-002",
        agent_id="codex",
        role="backend",
        action="implement_code",
        prompt_tokens=2000,
        completion_tokens=1000,
        run_id="run-test-1",
    )
    assert t2["total_tokens"] == 4500
    assert len(t2["spans"]) == 2
    assert "claude" in t2["cost_by_agent"]
    assert "codex" in t2["cost_by_agent"]

    # 3. Record Gemma turn (QA review)
    t3 = tracer.record_span(
        work_order_id="WO-001",
        span_id="op-003",
        agent_id="gemma",
        role="qa",
        action="qa_acceptance_test",
        prompt_tokens=800,
        completion_tokens=200,
        run_id="run-test-1",
    )
    assert t3["total_tokens"] == 5500
    assert len(t3["spans"]) == 3
    assert set(t3["cost_by_agent"].keys()) == {"claude", "codex", "gemma"}

    # File on disk matches
    trace_file = tmp_path / ".sync" / "traces" / "WO-001.json"
    assert trace_file.is_file()
    on_disk = json.loads(trace_file.read_text(encoding="utf-8"))
    assert on_disk["work_order_id"] == "WO-001"
    assert len(on_disk["spans"]) == 3


def test_work_order_tracer_idempotent_span_update(tmp_path: Path):
    """Recording the same span_id updates the existing span without duplicating."""
    tracer = WorkOrderTracer(tmp_path)
    tracer.record_span(
        work_order_id="WO-002",
        span_id="op-repeat",
        agent_id="codex",
        role="backend",
        prompt_tokens=500,
        completion_tokens=100,
        status="RUNNING",
    )
    # Update on completion
    updated = tracer.record_span(
        work_order_id="WO-002",
        span_id="op-repeat",
        agent_id="codex",
        role="backend",
        prompt_tokens=500,
        completion_tokens=250,
        status="COMPLETED",
    )
    assert len(updated["spans"]) == 1
    assert updated["spans"][0]["status"] == "COMPLETED"
    assert updated["spans"][0]["completion_tokens"] == 250


def test_run_cost_ledger_cross_work_order_aggregation(tmp_path: Path):
    """Tracer aggregates metrics across multiple work orders into run_cost_ledger.json."""
    tracer = WorkOrderTracer(tmp_path)

    # Record WO-001
    tracer.record_span("WO-001", "span-1", "claude", "architecture", prompt_tokens=1000, completion_tokens=500, run_id="run-100")
    tracer.record_span("WO-001", "span-2", "codex", "backend", prompt_tokens=2000, completion_tokens=1000, run_id="run-100")

    # Record WO-002
    tracer.record_span("WO-002", "span-3", "gemini", "frontend", prompt_tokens=1500, completion_tokens=800, run_id="run-100")
    tracer.record_span("WO-002", "span-4", "gemma", "qa", prompt_tokens=600, completion_tokens=150, run_id="run-100")

    ledger = tracer.get_run_ledger()
    assert ledger is not None
    assert ledger["run_id"] == "run-100"
    assert ledger["total_spans"] == 4
    assert ledger["work_order_traces"] == ["WO-001", "WO-002"]
    assert set(ledger["cost_by_agent"].keys()) == {"claude", "codex", "gemini", "gemma"}
    assert set(ledger["cost_by_work_order"].keys()) == {"WO-001", "WO-002"}

    # Markdown export
    md = tracer.export_summary_markdown()
    assert "# Execution Trace & Cost Ledger" in md
    assert "Cost by Agent" in md
    assert "Cost by Work Order" in md
    assert "`claude`" in md
    assert "`codex`" in md


def test_session_manager_auto_records_trace_on_complete(tmp_path: Path):
    """SessionManager.complete_operation automatically records trace spans for work orders."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session(
        agent="codex",
        provider="echo",
        contract={},
        workspace=str(tmp_path),
        session_id="sess-tr",
    )
    op = manager.start_turn("sess-tr", "Write API", work_order_id="WO-AUTO-01", agent_id="codex")
    op_id = op["operation_id"]

    manager.complete_operation(
        "sess-tr",
        op_id,
        result={"summary": "API implemented", "usage": {"prompt_tokens": 1200, "completion_tokens": 400}},
        status="COMPLETED",
    )

    tracer = WorkOrderTracer(tmp_path)
    trace = tracer.get_trace("WO-AUTO-01")
    assert trace is not None
    assert trace["work_order_id"] == "WO-AUTO-01"
    assert len(trace["spans"]) == 1
    span = trace["spans"][0]
    assert span["span_id"] == op_id
    assert span["agent_id"] == "codex"
    assert span["prompt_tokens"] == 1200
    assert span["completion_tokens"] == 400
