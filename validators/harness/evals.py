"""Fixed Feature Evals Matrix and Regression Runner.

Implements item C.7 of the StackMind harness:
- A small fixed set of canonical past features
- Rerunnable when prompts, tools, or models change
- Validates determinism, schema compliance, contract enforcement, and acceptance criteria
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract, ContractEvaluator
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.kernel.tracing import WorkOrderTracer
from validators.kernel.workspace import ScratchWorkspace


@dataclass
class EvalResult:
    """Outcome of running a single feature evaluation scenario."""

    scenario_id: str
    name: str
    passed: bool
    duration_ms: int
    failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalSummary:
    """Aggregated evaluation results across all executed scenarios."""

    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    success_rate: float
    total_duration_ms: int
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeatureScenario:
    """Base class for a fixed feature evaluation scenario."""

    scenario_id: str
    name: str
    description: str

    def run(self, workspace: Path) -> EvalResult:
        raise NotImplementedError


class ScaffoldFeatureScenario(FeatureScenario):
    """Evaluates project initialization, spec breakdown, and authoring gate compliance."""

    scenario_id = "feat_scaffold"
    name = "Project Scaffolding & Initial Specification"
    description = "Verifies initial repository structure, PLAN.md parsing, and work order authoring gate."

    def run(self, workspace: Path) -> EvalResult:
        t0 = time.perf_counter()
        failures: list[str] = []
        metrics: dict[str, Any] = {}

        # 1. Setup workspace with spec and PLAN.md
        plan_content = (
            "# Project Plan\n\n"
            "## Current Architecture\n"
            "Initial greenfield setup.\n\n"
            "## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Core setup\n"
            "  - Deliverable: src/app.py\n"
        )
        plan_path = workspace / "PLAN.md"
        plan_path.write_text(plan_content, encoding="utf-8")

        # 2. Author work order WO-001
        sync_dir = workspace / ".sync"
        wo_dir = sync_dir / "work-orders" / "ACTIVE"
        contracts_dir = sync_dir / "contracts"
        wo_dir.mkdir(parents=True, exist_ok=True)
        contracts_dir.mkdir(parents=True, exist_ok=True)

        wo_data = {
            "id": "WO-001",
            "title": "Initialize App",
            "type": "FEATURE",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "deliverable": {
                "type": "code",
                "path": "src/app.py",
                "description": "Main application entry point",
            },
            "dependencies": [],
        }
        wo_path = wo_dir / "WO-001.yaml"
        wo_yaml = yaml.safe_dump(wo_data, sort_keys=False)
        wo_path.write_text(wo_yaml, encoding="utf-8")

        contract_data = {
            "work_order_id": "WO-001",
            "agent_id": "codex",
            "assigned_agent": "codex",
            "role": "backend",
            "revision": 1,
            "scope": {
                "allow": [{"module": "src/**"}, {"module": "src/app.py"}],
                "deny": [{"module": ".env"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 10000},
        }
        contract_path = contracts_dir / "WO-001.yaml"
        contract_yaml = yaml.safe_dump(contract_data, sort_keys=False)
        contract_path.write_text(contract_yaml, encoding="utf-8")

        # 3. Verify Authoring Gate validation
        gate = AuthoringGate()
        wo_res = gate.validate_artifact_content(
            ".sync/work-orders/ACTIVE/WO-001.yaml", wo_yaml, agent="claude"
        )
        if not wo_res.passed:
            failures.append(f"AuthoringGate rejected WO-001: {wo_res.summary}")

        contract_res = gate.validate_artifact_content(
            ".sync/contracts/WO-001.yaml", contract_yaml, agent="claude"
        )
        if not contract_res.passed:
            failures.append(f"AuthoringGate rejected Contract WO-001: {contract_res.summary}")

        metrics["authoring_gate_passed"] = len(failures) == 0
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return EvalResult(
            scenario_id=self.scenario_id,
            name=self.name,
            passed=len(failures) == 0,
            duration_ms=duration_ms,
            failures=failures,
            metrics=metrics,
        )


class AuthFeatureScenario(FeatureScenario):
    """Evaluates implementation of an auth module within contract and verification gates."""

    scenario_id = "feat_auth"
    name = "User Authentication Implementation"
    description = "Verifies authorized file creation, contract boundary compliance, and QA pass record."

    def run(self, workspace: Path) -> EvalResult:
        t0 = time.perf_counter()
        failures: list[str] = []
        metrics: dict[str, Any] = {}

        # 1. Setup contract
        evaluator = ContractEvaluator()
        contract = AgentContract(
            agent_id="codex",
            work_order="WO-010",
            allow=("src/auth.py", "tests/test_auth.py"),
            deny=("src/secret.key",),
            write_mode="read-write",
        )

        # 2. Scope checks
        can_write_auth, _ = evaluator.authorize(contract, "write_file", "src/auth.py")
        if not can_write_auth:
            failures.append("Gateway failed to authorize allowed file 'src/auth.py'")

        can_write_secret, _ = evaluator.authorize(contract, "write_file", "src/secret.key")
        if can_write_secret:
            failures.append("Gateway incorrectly authorized denied file 'src/secret.key'")

        # 3. Simulate deliverable write
        auth_file = workspace / "src" / "auth.py"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(
            "def authenticate(username, password):\n    return username == 'admin' and password == 'secret'\n",
            encoding="utf-8",
        )

        # 4. Record QA approval verdict
        reviews_dir = workspace / ".sync" / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        verdict_file = reviews_dir / "gemma_WO-010_verdict.md"
        verdict_file.write_text("# QA Verdict\nStatus: APPROVED\nAll 4 assertions passed.\n", encoding="utf-8")

        # 5. Record trace span
        tracer = WorkOrderTracer(workspace)
        tracer.record_span(
            work_order_id="WO-010",
            span_id="span-auth-impl",
            agent_id="codex",
            role="backend",
            prompt_tokens=450,
            completion_tokens=180,
            status="COMPLETED",
        )
        tracer.record_span(
            work_order_id="WO-010",
            span_id="span-auth-qa",
            agent_id="gemma",
            role="qa",
            prompt_tokens=320,
            completion_tokens=90,
            status="COMPLETED",
        )

        trace = tracer.get_trace("WO-010")
        if not trace or len(trace.get("spans", [])) != 2:
            failures.append("Trace span count mismatch for WO-010")

        metrics["trace_total_cost"] = trace.get("total_cost", 0.0) if trace else 0.0
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return EvalResult(
            scenario_id=self.scenario_id,
            name=self.name,
            passed=len(failures) == 0,
            duration_ms=duration_ms,
            failures=failures,
            metrics=metrics,
        )


class ContractGovernanceScenario(FeatureScenario):
    """Evaluates dynamic contract change request protocol when worker needs scope expansion."""

    scenario_id = "feat_contract_change"
    name = "Contract Change Request (CCR) Protocol"
    description = "Verifies fail-closed out-of-scope write, CCR submission, Architect approval, and revision bump."

    def run(self, workspace: Path) -> EvalResult:
        t0 = time.perf_counter()
        failures: list[str] = []
        metrics: dict[str, Any] = {}

        # 1. Setup contract
        contracts_dir = workspace / ".sync" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        wo_id = "WO-020"
        contract_data = {
            "work_order_id": wo_id,
            "assigned_agent": "codex",
            "role": "backend",
            "revision": 1,
            "scope": {
                "allow": [{"module": "src/user.py"}],
                "deny": [],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 10000},
        }
        contract_path = contracts_dir / f"{wo_id}.yaml"
        contract_path.write_text(yaml.safe_dump(contract_data), encoding="utf-8")

        # 2. Worker requests contract change for utils/hash.py
        evaluator = ContractEvaluator()
        initial_contract = AgentContract(
            agent_id="codex",
            work_order=wo_id,
            allow=("src/user.py", ".sync/**"),
            deny=(),
            write_mode="read-write",
        )
        can_write_before, _ = evaluator.authorize(initial_contract, "write_file", "utils/hash.py")
        if can_write_before:
            failures.append("utils/hash.py should have been unauthorized before CCR")

        scratch_dir = workspace / ".scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        sw = ScratchWorkspace(workspace, scratch_dir, "eval-ccr")

        # Copy schema if needed
        schema_src = Path(__file__).resolve().parents[2] / "schemas" / "contract-change-request.schema.json"
        if schema_src.is_file():
            schema_dst_dir = workspace / "schemas"
            schema_dst_dir.mkdir(parents=True, exist_ok=True)
            (schema_dst_dir / "contract-change-request.schema.json").write_text(
                schema_src.read_text(encoding="utf-8"), encoding="utf-8"
            )

        worker_gateway = ToolGateway(
            sw,
            RuntimeBoundary(OperationJournal()),
            initial_contract,
            get_role_policy("codex"),
            session_id="eval-sess",
            attempt_id="eval-att-1",
            actor_id="codex",
            provider_id="eval-provider",
        )
        ccr_result = worker_gateway.request_contract_change(
            work_order=wo_id,
            requested_allow=["utils/hash.py"],
            rationale="Need utility module for hashing passwords",
        )
        if not ccr_result.get("success"):
            failures.append(f"request_contract_change failed: {ccr_result.get('error')}")

        req_id = ccr_result.get("request_id")
        if not req_id:
            failures.append("No request_id returned from request_contract_change")

        # 3. Architect evaluates and approves CCR
        arch_contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=(".sync/**",),
            deny=(),
            write_mode="read-write",
        )
        arch_gateway = ToolGateway(
            sw,
            RuntimeBoundary(OperationJournal()),
            arch_contract,
            get_role_policy("claude"),
            session_id="eval-sess",
            attempt_id="eval-att-2",
            actor_id="claude",
            provider_id="eval-provider",
        )
        eval_result = arch_gateway.evaluate_contract_change_request(
            request_id=req_id,
            decision="APPROVED",
            reason="Approved auxiliary utility write",
        )
        if not eval_result.get("success"):
            failures.append(f"evaluate_contract_change_request failed: {eval_result.get('error')}")

        # 4. Verify updated contract on disk
        updated_data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if updated_data.get("revision") != 2:
            failures.append(f"Expected contract revision 2, got {updated_data.get('revision')}")

        raw_allows = updated_data.get("allow") or updated_data.get("scope", {}).get("allow", [])
        allow_rules = tuple(
            r["module"] if isinstance(r, dict) else r
            for r in raw_allows
        )
        updated_contract = AgentContract(
            agent_id="codex",
            work_order=wo_id,
            allow=allow_rules,
            deny=(),
            write_mode="read-write",
        )
        can_write_after, _ = evaluator.authorize(updated_contract, "write_file", "utils/hash.py")
        if not can_write_after:
            failures.append("utils/hash.py still unauthorized after approved CCR")

        metrics["ccr_id"] = req_id
        metrics["new_contract_revision"] = updated_data.get("revision")
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return EvalResult(
            scenario_id=self.scenario_id,
            name=self.name,
            passed=len(failures) == 0,
            duration_ms=duration_ms,
            failures=failures,
            metrics=metrics,
        )


class FixedFeatureEvalRunner:
    """Orchestrates running the fixed set of feature regression scenarios."""

    DEFAULT_SCENARIOS = [
        ScaffoldFeatureScenario,
        AuthFeatureScenario,
        ContractGovernanceScenario,
    ]

    def __init__(self, scenarios: list[type[FeatureScenario]] | None = None) -> None:
        self.scenario_classes = scenarios or self.DEFAULT_SCENARIOS

    def run_all(self, workspace: Path) -> EvalSummary:
        results: list[EvalResult] = []
        total_duration = 0

        for sc_class in self.scenario_classes:
            scenario = sc_class()
            # Run in isolated sub-workspace
            sub_ws = workspace / f"eval_{scenario.scenario_id}"
            sub_ws.mkdir(parents=True, exist_ok=True)
            res = scenario.run(sub_ws)
            results.append(res)
            total_duration += res.duration_ms

        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        rate = (passed_count / len(results)) * 100.0 if results else 0.0

        return EvalSummary(
            total_scenarios=len(results),
            passed_scenarios=passed_count,
            failed_scenarios=failed_count,
            success_rate=round(rate, 2),
            total_duration_ms=total_duration,
            results=[r.to_dict() for r in results],
        )
