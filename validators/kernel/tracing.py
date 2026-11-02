"""Work Order Tracing and Cost Ledger.

Implements item C.6 of the StackMind harness:
- One correlation trace per work order across all participating agents
- Spans recorded for Architect, Worker, QA, and GitOps turns
- Token consumption and cost accounting per agent and per run
- Atomic persistence to .sync/traces/{work_order_id}.json and .sync/traces/run_cost_ledger.json
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# Default pricing rates in USD per 1,000 tokens (prompt, completion)
DEFAULT_AGENT_PRICING: dict[str, dict[str, float]] = {
    "claude": {"prompt": 0.003, "completion": 0.015},      # Architect / Orchestration
    "codex": {"prompt": 0.0015, "completion": 0.002},      # Backend implementation
    "gemini": {"prompt": 0.001, "completion": 0.002},       # Frontend / General
    "gemma": {"prompt": 0.0002, "completion": 0.0004},     # QA Review & Testing
    "local-llm": {"prompt": 0.0, "completion": 0.0},        # GitOps release / deterministic
    "default": {"prompt": 0.001, "completion": 0.002},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_token_cost(
    agent_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    custom_pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Calculate USD cost from tokens using per-agent rates."""
    pricing = (custom_pricing or DEFAULT_AGENT_PRICING).get(
        agent_id.lower(),
        (custom_pricing or DEFAULT_AGENT_PRICING).get("default", {"prompt": 0.001, "completion": 0.002}),
    )
    prompt_rate = pricing.get("prompt", 0.001)
    completion_rate = pricing.get("completion", 0.002)
    cost = (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate
    return round(cost, 6)


@dataclass
class TraceSpan:
    """A single execution turn or operation span within a work order."""

    span_id: str
    work_order_id: str
    agent_id: str
    role: str
    action: str
    status: str
    started_at: str
    completed_at: str
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkOrderTracer:
    """Manages work order traces and the global run cost ledger."""

    def __init__(self, workspace: Path | str, pricing: dict[str, dict[str, float]] | None = None) -> None:
        self.workspace = Path(workspace)
        self.traces_dir = self.workspace / ".sync" / "traces"
        self.pricing = pricing or DEFAULT_AGENT_PRICING

    def _ensure_dir(self) -> None:
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def _trace_file(self, work_order_id: str) -> Path:
        return self.traces_dir / f"{work_order_id}.json"

    def _ledger_file(self) -> Path:
        return self.traces_dir / "run_cost_ledger.json"

    def record_span(
        self,
        work_order_id: str,
        span_id: str,
        agent_id: str,
        role: str,
        action: str = "turn",
        status: str = "COMPLETED",
        started_at: str | None = None,
        completed_at: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float | None = None,
        model: str | None = None,
        run_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an execution span for a work order and update the run ledger."""
        self._ensure_dir()
        now = _now_iso()
        start = started_at or now
        end = completed_at or now

        # Calculate duration
        duration_ms = 0
        try:
            t0 = datetime.fromisoformat(start)
            t1 = datetime.fromisoformat(end)
            duration_ms = max(0, int((t1 - t0).total_seconds() * 1000))
        except Exception:
            duration_ms = 0

        tot_tokens = prompt_tokens + completion_tokens
        if cost is None:
            cost = compute_token_cost(agent_id, prompt_tokens, completion_tokens, self.pricing)

        span = TraceSpan(
            span_id=span_id,
            work_order_id=work_order_id,
            agent_id=agent_id,
            role=role,
            action=action,
            status=status,
            started_at=start,
            completed_at=end,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=tot_tokens,
            cost=round(cost, 6),
            model=model,
            meta=dict(meta or {}),
        )

        trace = self._load_or_init_trace(work_order_id, run_id or "run-unknown")
        
        # Idempotently update or append span
        existing_spans = trace.get("spans", [])
        updated = False
        for idx, s in enumerate(existing_spans):
            if s.get("span_id") == span_id:
                existing_spans[idx] = span.to_dict()
                updated = True
                break
        if not updated:
            existing_spans.append(span.to_dict())

        trace["spans"] = existing_spans
        trace["updated_at"] = now
        self._recompute_trace_aggregates(trace)

        # Write trace atomically
        t_file = self._trace_file(work_order_id)
        tmp_file = t_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(trace, indent=2), encoding="utf-8")
        tmp_file.replace(t_file)

        # Update run cost ledger
        self._update_run_ledger(run_id or trace.get("run_id", "run-unknown"))

        return trace

    def _load_or_init_trace(self, work_order_id: str, run_id: str) -> dict[str, Any]:
        t_file = self._trace_file(work_order_id)
        if t_file.is_file():
            try:
                data = json.loads(t_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        now = _now_iso()
        return {
            "work_order_id": work_order_id,
            "run_id": run_id,
            "created_at": now,
            "updated_at": now,
            "total_cost": 0.0,
            "total_tokens": 0,
            "total_duration_ms": 0,
            "cost_by_agent": {},
            "tokens_by_agent": {},
            "duration_by_agent_ms": {},
            "spans": [],
        }

    def _recompute_trace_aggregates(self, trace: dict[str, Any]) -> None:
        spans = trace.get("spans", [])
        total_cost = 0.0
        total_tokens = 0
        total_duration = 0
        cost_by_agent: dict[str, float] = {}
        tokens_by_agent: dict[str, int] = {}
        duration_by_agent: dict[str, int] = {}

        for s in spans:
            agent = str(s.get("agent_id", "unknown")).lower()
            cost = float(s.get("cost", 0.0))
            tokens = int(s.get("total_tokens", 0))
            dur = int(s.get("duration_ms", 0))

            total_cost += cost
            total_tokens += tokens
            total_duration += dur

            cost_by_agent[agent] = round(cost_by_agent.get(agent, 0.0) + cost, 6)
            tokens_by_agent[agent] = tokens_by_agent.get(agent, 0) + tokens
            duration_by_agent[agent] = duration_by_agent.get(agent, 0) + dur

        trace["total_cost"] = round(total_cost, 6)
        trace["total_tokens"] = total_tokens
        trace["total_duration_ms"] = total_duration
        trace["cost_by_agent"] = cost_by_agent
        trace["tokens_by_agent"] = tokens_by_agent
        trace["duration_by_agent_ms"] = duration_by_agent

    def get_trace(self, work_order_id: str) -> dict[str, Any] | None:
        """Retrieve trace for a given work order."""
        t_file = self._trace_file(work_order_id)
        if not t_file.is_file():
            return None
        try:
            return json.loads(t_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_traces(self) -> list[dict[str, Any]]:
        """List all work order traces in workspace."""
        if not self.traces_dir.is_dir():
            return []
        traces = []
        for p in sorted(self.traces_dir.glob("*.json")):
            if p.name == "run_cost_ledger.json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "work_order_id" in data:
                    traces.append(data)
            except Exception:
                pass
        return traces

    def _update_run_ledger(self, run_id: str) -> dict[str, Any]:
        """Aggregate all work order traces into the workspace run cost ledger."""
        self._ensure_dir()
        traces = self.list_traces()

        total_cost = 0.0
        total_tokens = 0
        total_spans = 0
        cost_by_agent: dict[str, float] = {}
        cost_by_wo: dict[str, float] = {}
        tokens_by_agent: dict[str, int] = {}
        tokens_by_wo: dict[str, int] = {}
        work_orders: list[str] = []

        for t in traces:
            wo_id = t.get("work_order_id", "unknown")
            work_orders.append(wo_id)
            wo_cost = float(t.get("total_cost", 0.0))
            wo_tokens = int(t.get("total_tokens", 0))

            total_cost += wo_cost
            total_tokens += wo_tokens
            cost_by_wo[wo_id] = round(wo_cost, 6)
            tokens_by_wo[wo_id] = wo_tokens
            total_spans += len(t.get("spans", []))

            for agent, c in t.get("cost_by_agent", {}).items():
                cost_by_agent[agent] = round(cost_by_agent.get(agent, 0.0) + float(c), 6)
            for agent, tok in t.get("tokens_by_agent", {}).items():
                tokens_by_agent[agent] = tokens_by_agent.get(agent, 0) + int(tok)

        ledger = {
            "run_id": run_id,
            "updated_at": _now_iso(),
            "total_cost": round(total_cost, 6),
            "total_tokens": total_tokens,
            "total_spans": total_spans,
            "cost_by_agent": cost_by_agent,
            "tokens_by_agent": tokens_by_agent,
            "cost_by_work_order": cost_by_wo,
            "tokens_by_work_order": tokens_by_wo,
            "work_order_traces": sorted(set(work_orders)),
        }

        l_file = self._ledger_file()
        tmp_file = l_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        tmp_file.replace(l_file)
        return ledger

    def get_run_ledger(self) -> dict[str, Any] | None:
        """Retrieve the persisted run cost ledger."""
        l_file = self._ledger_file()
        if not l_file.is_file():
            return None
        try:
            return json.loads(l_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def export_summary_markdown(self) -> str:
        """Generate a human-readable markdown cost and trace summary."""
        ledger = self.get_run_ledger() or {}
        lines = [
            f"# Execution Trace & Cost Ledger (Run: {ledger.get('run_id', 'unknown')})",
            "",
            f"- **Total Run Cost**: ${ledger.get('total_cost', 0.0):.4f}",
            f"- **Total Tokens**: {ledger.get('total_tokens', 0):,}",
            f"- **Total Spans**: {ledger.get('total_spans', 0)}",
            "",
            "## Cost by Agent",
            "",
            "| Agent | Cost (USD) | Tokens |",
            "|---|---|---|",
        ]
        for agent, c in sorted(ledger.get("cost_by_agent", {}).items()):
            tok = ledger.get("tokens_by_agent", {}).get(agent, 0)
            lines.append(f"| `{agent}` | ${c:.4f} | {tok:,} |")

        lines.extend([
            "",
            "## Cost by Work Order",
            "",
            "| Work Order | Cost (USD) | Tokens |",
            "|---|---|---|",
        ])
        for wo, c in sorted(ledger.get("cost_by_work_order", {}).items()):
            tok = ledger.get("tokens_by_work_order", {}).get(wo, 0)
            lines.append(f"| `{wo}` | ${c:.4f} | {tok:,} |")

        return "\n".join(lines) + "\n"


def record_operation_span(workspace: Path | str, session: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Helper invoked by SessionManager to record a completed operation span."""
    wo_id = record.get("work_order_id") or record.get("work_order")
    if not wo_id:
        return None

    raw_agent = str(record.get("agent_id") or record.get("role") or session.get("agent") or "unknown").lower().strip()
    op_id = str(record.get("operation_id", "span-unknown"))
    role = str(record.get("role") or session.get("role") or "backend")
    status = str(record.get("status", "COMPLETED"))
    started_at = record.get("started_at")
    completed_at = record.get("completed_at")
    run_id = record.get("run_id") or session.get("run_id") or session.get("session_id") or "run-default"

    # Extract tokens from telemetry/usage metadata
    result = record.get("result") or {}
    telemetry = result.get("telemetry") or {} if isinstance(result, Mapping) else {}
    usage = result.get("usage") or {} if isinstance(result, Mapping) else {}
    if not usage and isinstance(telemetry, Mapping):
        usage = telemetry.get("usage") or {}

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    # Fallback to estimating tokens if zero
    if prompt_tokens == 0 and completion_tokens == 0:
        prompt_txt = str(record.get("prompt") or "")
        summary_txt = str(result.get("summary") or result.get("report_markdown") or "") if isinstance(result, Mapping) else ""
        if prompt_txt:
            prompt_tokens = max(10, len(prompt_txt.split()) * 2)
        if summary_txt:
            completion_tokens = max(10, len(summary_txt.split()) * 2)

    tracer = WorkOrderTracer(workspace)
    return tracer.record_span(
        work_order_id=str(wo_id),
        span_id=op_id,
        agent_id=raw_agent,
        role=role,
        action=str(record.get("operation", "turn")),
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=result.get("model") if isinstance(result, Mapping) else None,
        run_id=str(run_id),
        meta={"operation_id": op_id},
    )
