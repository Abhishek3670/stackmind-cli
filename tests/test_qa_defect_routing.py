"""QA defect routing: a QA turn blocked on failing tests must route the defect
back to the developer work order instead of retrying QA on the same failing
suite (which can only trip the loop guard again).

Covers:
1. Routing on a named verdict: a NEEDS_CHANGES verdict file naming a completed
   work order re-opens that work order and dispatches a rework turn.
2. Hold-back: the QA work order is not re-dispatched until every rework target
   has re-completed; after rework, a fresh QA turn is dispatched.
3. Routing on deliverable-path evidence when no verdict file exists, while the
   QA work order's own test suite is never chosen as a rework target.
4. Governance failures (contract violations) are NOT defect-routed — they keep
   the Architect recovery path.
5. RunState roundtrip preserves qa_rework_targets.
6. The runner's QA system prompt carries the scan-ordering and defect
   finalization doctrine, and is wired into the gemma turn's system message.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.harness.runner import (
    AgentRunner,
    GovernedToolRuntime,
    HarnessTask,
    LLMRequest,
    QA_SYSTEM_PROMPT,
    EchoLLMProvider,
)
from validators.harness.retrieval import RetrievalBatch
from validators.kernel.providers.models import Message


def _write_worker_yaml(ws: Path, wo_id: str, agent: str, deliverable: str | None) -> None:
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": f"Task {wo_id}",
        "status": "ACTIVE",
        "assigned_agents": [agent],
        "dependencies": [],
        "deliverable": {"type": "code", "description": f"{wo_id} deliverable"},
    }
    if deliverable:
        record["deliverable"]["path"] = deliverable
    (wo_dir / f"{wo_id}.yaml").write_text(
        yaml.safe_dump(record, sort_keys=False), encoding="utf-8"
    )


def _write_contract_yaml(ws: Path, wo_id: str, agent: str) -> None:
    contract_dir = ws / ".sync" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "identity": {"role": "qa", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "tests/**"}, {"module": ".sync/inbox/claude/**"}],
            "deny": [],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 0},
    }
    (contract_dir / f"{wo_id}.yaml").write_text(
        yaml.safe_dump(record, sort_keys=False), encoding="utf-8"
    )


def _make_state(tmp_path: Path, worker_wo_ids: list[str]) -> tuple[LifecycleSupervisor, MockSessionManager, RunState]:
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Build landing page", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = list(worker_wo_ids)
    state.gitops_wo_id = "WO-005"
    return supervisor, mock_mgr, state


def test_qa_blocked_turn_routes_defect_to_developer(tmp_path: Path) -> None:
    """A blocked gemma turn with a NEEDS_CHANGES verdict naming WO-001 re-opens
    WO-001 with a rework turn instead of retrying QA."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    reviews = tmp_path / ".sync" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    verdict = reviews / "WO-001-qa-verdict.md"
    verdict.write_text(
        "Verdict: NEEDS_CHANGES\nFailing: index.html is missing element with class 'welcome-text'\n",
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-004", "WO-001"])
    state.completed_wo_ids = ["WO-001"]

    wo1_op = mock_mgr.start_turn("sess-001", "Build page", work_order_id="WO-001", agent_id="gemini")
    mock_mgr.complete_operation(wo1_op["operation_id"], "COMPLETED")
    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"],
        "BLOCKED",
        result={
            "status": "blocked",
            "summary": "QA found an application defect",
            "blockers": ["index.html missing element with class 'welcome-text'"],
            "commands_audit": [{"command": "pytest tests/test_animations.py", "returncode": 1}],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}
    assert "WO-001" not in state.completed_wo_ids
    # The blocked QA turn and the stale completed developer turn are consumed.
    assert wo4_op["operation_id"] in state.ignored_operation_ids
    assert wo1_op["operation_id"] in state.ignored_operation_ids
    # A rework turn was dispatched for the developer work order.
    rework_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("work_order_id") == "WO-001" and op["operation_id"] != wo1_op["operation_id"]
    ]
    assert len(rework_ops) == 1
    assert "Rework work order WO-001" in rework_ops[0]["prompt"]
    assert "index.html" in rework_ops[0]["prompt"]
    assert rework_ops[0]["agent_id"] == "gemini"
    assert state.retry_counts.get("WO-001") == 1
    # The consumed verdict is archived so it cannot re-trigger rework.
    assert not verdict.exists()
    consumed = list((reviews / "_consumed").glob("WO-001-qa-verdict*"))
    assert len(consumed) == 1
    # Both work orders are ACTIVE on disk for re-dispatch.
    wo1 = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text())
    wo4 = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-004.yaml").read_text())
    assert wo1["status"] == "ACTIVE"
    assert wo4["status"] == "ACTIVE"


def test_qa_redispatch_waits_for_rework_completion(tmp_path: Path) -> None:
    """The QA work order stays held back until every rework target re-completes;
    then a fresh QA turn is dispatched."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    reviews = tmp_path / ".sync" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / "WO-001-qa-verdict.md").write_text(
        "Verdict: NEEDS_CHANGES\nindex.html defect\n", encoding="utf-8"
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-004", "WO-001"])
    state.completed_wo_ids = ["WO-001"]
    wo1_op = mock_mgr.start_turn("sess-001", "Build page", work_order_id="WO-001", agent_id="gemini")
    mock_mgr.complete_operation(wo1_op["operation_id"], "COMPLETED")
    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"], "BLOCKED",
        result={"status": "blocked", "summary": "QA found defects", "blockers": ["index.html broken"]},
    )

    supervisor.advance(state)  # routes the defect
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}
    ops_before = len(mock_mgr.list_operations())

    # While rework is still in flight, the QA work order must not be re-dispatched.
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert len(mock_mgr.list_operations()) == ops_before
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}

    # The rework turn completes; the next advance re-completes WO-001.
    rework_op = next(
        op for op in reversed(mock_mgr.list_operations())
        if op.get("work_order_id") == "WO-001" and op["operation_id"] != wo1_op["operation_id"]
    )
    mock_mgr.complete_operation(
        rework_op["operation_id"], "COMPLETED",
        result={"status": "completed", "summary": "fixed index.html", "modified_files": ["index.html"]},
    )
    supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids

    # Now the QA work order is re-dispatched fresh.
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.qa_rework_targets == {}
    qa_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("work_order_id") == "WO-004" and op["operation_id"] != wo4_op["operation_id"]
    ]
    assert len(qa_ops) == 1
    assert qa_ops[0]["agent_id"] == "gemma"


def test_qa_blocked_without_verdict_routes_by_deliverable_path(tmp_path: Path) -> None:
    """Without a verdict file, the deliverable path named in the blocker routes
    the defect; the QA work order's own test suite is never a target."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-004", "WO-001"])
    state.completed_wo_ids = ["WO-001"]
    wo1_op = mock_mgr.start_turn("sess-001", "Build page", work_order_id="WO-001", agent_id="gemini")
    mock_mgr.complete_operation(wo1_op["operation_id"], "COMPLETED")
    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"], "BLOCKED",
        result={
            "status": "blocked",
            "summary": "test_animations.py failed: Element with class 'welcome-text' not found in index.html",
            "blockers": ["AssertionError in test_animations.py: element missing in index.html"],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}
    rework_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("work_order_id") == "WO-001" and op["operation_id"] != wo1_op["operation_id"]
    ]
    assert len(rework_ops) == 1


def test_qa_governance_failure_keeps_architect_recovery_path(tmp_path: Path) -> None:
    """A contract-violation evidence packet is governance, not a defect finding:
    it must keep routing to the Architect recovery decision."""
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    _write_contract_yaml(tmp_path, "WO-004", "gemma")

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-004"])
    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"], "BLOCKED",
        result={
            "status": "blocked",
            "summary": "outside contract scope",
            "blockers": [],
            "failure": {
                "failure_code": "CONTRACT_SCOPE_VIOLATION",
                "canonical_message": "CONTRACT_SCOPE_VIOLATION: wrote outside allow scope",
            },
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.qa_rework_targets == {}


def test_run_state_roundtrip_preserves_qa_rework_targets() -> None:
    state = RunState(
        run_id="run-rt", product_goal="goal", workspace="ws", session_id="sess",
    )
    state.qa_rework_targets = {"WO-004": ["WO-001", "WO-002"]}
    restored = RunState.from_dict(state.to_dict())
    assert restored.qa_rework_targets == {"WO-004": ["WO-001", "WO-002"]}


def test_qa_system_prompt_carries_defect_doctrine() -> None:
    text = QA_SYSTEM_PROMPT
    # Scan ordering: immediately after run_tests, regardless of outcome.
    assert "IMMEDIATELY after `run_tests`" in text
    assert "run_security_scan" in text
    assert "never skip it because tests already failed" in text
    # Defect finalization: verdict + blocked-with-path instead of retry loops.
    assert "NEEDS_CHANGES" in text
    assert ".sync/inbox/claude/" in text
    assert "status 'blocked'" in text
    assert "Do NOT edit application source code" in text
    assert "Do NOT rewrite or re-run the test suite more than once" in text
    assert "The supervisor routes the fix back to the developer agent" in text


def test_qa_turn_system_message_is_the_compiled_doctrine(tmp_path: Path, monkeypatch: Any) -> None:
    """The gemma branch wires QA_SYSTEM_PROMPT into the turn's system message."""
    from validators.kernel.providers.gateway import ProviderGateway

    captured: dict[str, Any] = {}

    def fake_gateway_run_loop(self: Any, messages: list[Message], **kwargs: Any) -> list[Message]:
        captured["messages"] = list(messages)
        captured["kwargs"] = kwargs
        return [
            Message.assistant(json.dumps({
                "status": "blocked",
                "summary": "QA found an application defect in index.html",
                "report_markdown": "index.html is missing .welcome-text",
                "modified_files": ["tests/test_animations.py"],
                "blockers": ["index.html missing element with class 'welcome-text'"],
            })),
        ]

    monkeypatch.setattr(ProviderGateway, "run_loop", fake_gateway_run_loop)

    stub_gateway = SimpleNamespace(run_loop=fake_gateway_run_loop, written_files=[], contract=None)
    runtime = GovernedToolRuntime(
        workspace=SimpleNamespace(root=tmp_path, attempt_id="attempt-test"), gateway=stub_gateway,
    )
    runner = AgentRunner(tmp_path, "gemma", llm_provider=EchoLLMProvider())
    task = HarnessTask(
        kind="work_order",
        identifier="WO-004",
        path=tmp_path,
        title="QA animations",
        body="",
        query="",
        work_order_id="WO-004",
        deliverable_path="tests/test_animations.py",
    )
    request = LLMRequest(
        agent="gemma",
        session_count=1,
        task=task,
        context=None,
        retrieval=RetrievalBatch(
            results=(), evidence=(), searches_used=0, cache_hits=0,
            cap_exhausted=False, mode="internal_only", cost_estimate=0.0,
        ),
    )

    record = runner._complete_request(request, runtime)

    messages = captured["messages"]
    assert messages[0].content == QA_SYSTEM_PROMPT
    assert captured["kwargs"]["required_deliverable"] == "tests/test_animations.py"
    assert record.payload["status"] == "blocked"
    assert record.payload["blockers"], "defect finding must surface as a blocker"


def test_qa_completed_turn_with_defect_verdict_routes_to_developer(tmp_path: Path) -> None:
    """When a gemma turn completes its tools with status 'completed' but authored a
    NEEDS_CHANGES verdict naming WO-001, the supervisor routes the defect to WO-001
    instead of completing without rework."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    reviews = tmp_path / ".sync" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    verdict = reviews / "WO-001-qa-verdict.md"
    verdict.write_text(
        "Verdict: NEEDS_CHANGES\nDeliverable: index.html missing .welcome-text\n",
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-004", "WO-001"])
    state.completed_wo_ids = ["WO-001"]

    wo1_op = mock_mgr.start_turn("sess-001", "Build page", work_order_id="WO-001", agent_id="gemini")
    mock_mgr.complete_operation(wo1_op["operation_id"], "COMPLETED")
    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"],
        "COMPLETED",
        result={
            "status": "completed",
            "summary": "QA completed turn via tools",
            "commands_audit": [],
            "tool_calls_audit": [
                {"tool": "run_tests"},
                {"tool": "run_security_scan"},
            ],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}
    assert "WO-001" not in state.completed_wo_ids
    assert wo4_op["operation_id"] in state.ignored_operation_ids
    assert wo1_op["operation_id"] in state.ignored_operation_ids
    rework_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("work_order_id") == "WO-001" and op["operation_id"] != wo1_op["operation_id"]
    ]
    assert len(rework_ops) == 1
    assert "Rework work order WO-001" in rework_ops[0]["prompt"]


def test_qa_work_order_never_retries_itself_on_qa_verdict(tmp_path: Path) -> None:
    """A QA work order must never evaluate _check_qa_verdict against itself, preventing
    infinite self-retry loops when a verdict file references the QA work order ID."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    claude_inbox = tmp_path / ".sync" / "inbox" / "claude"
    claude_inbox.mkdir(parents=True, exist_ok=True)
    # A verdict file named with the QA WO ID itself
    verdict = claude_inbox / "WO-004-qa-verdict.md"
    verdict.write_text(
        "# QA Verdict for WO-004\n## Status: NEEDS_CHANGES\nDependency missing in environment\n",
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001", "WO-004"])
    state.completed_wo_ids = ["WO-001"]

    # _check_qa_verdict on the QA work order must return None
    assert supervisor._check_qa_verdict("WO-004", tmp_path) is None

    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"],
        "COMPLETED",
        result={
            "status": "completed",
            "summary": "QA completed",
            "tool_calls_audit": [
                {"tool": "run_tests"},
                {"tool": "run_security_scan"},
            ],
        },
    )

    res = supervisor.advance(state)

    # QA completes without re-triggering itself on its own verdict file
    assert "WO-004" in state.completed_wo_ids
    assert state.retry_counts.get("WO-004", 0) == 0


def test_qa_completed_turn_with_deliverable_defect_routes_rework_to_developer(tmp_path: Path) -> None:
    """When a QA turn completes but authored a NEEDS_CHANGES verdict naming a deliverable path
    (e.g. index.html) instead of an explicit WO ID, supervisor routes rework to the developer WO
    owning index.html (WO-001)."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    claude_inbox = tmp_path / ".sync" / "inbox" / "claude"
    claude_inbox.mkdir(parents=True, exist_ok=True)
    verdict = claude_inbox / "WO-004-qa-verdict.md"
    verdict.write_text(
        "# QA Verdict: WO-004\n## Status: NEEDS_CHANGES\n"
        "Blocker: index.html is missing element with id='animated-text'\n",
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001", "WO-004"])
    state.completed_wo_ids = ["WO-001"]

    wo1_op = mock_mgr.start_turn("sess-001", "Build page", work_order_id="WO-001", agent_id="gemini")
    mock_mgr.complete_operation(wo1_op["operation_id"], "COMPLETED")

    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"],
        "COMPLETED",
        result={
            "status": "completed",
            "summary": "QA executed tests",
            "tool_calls_audit": [
                {"tool": "run_tests"},
                {"tool": "run_security_scan"},
            ],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.qa_rework_targets == {"WO-004": ["WO-001"]}
    assert "WO-001" not in state.completed_wo_ids
    assert "WO-004" not in state.completed_wo_ids
    rework_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("work_order_id") == "WO-001" and op["operation_id"] != wo1_op["operation_id"]
    ]
    assert len(rework_ops) == 1
    assert "Rework work order WO-001" in rework_ops[0]["prompt"]
    assert "index.html" in rework_ops[0]["prompt"]


def test_qa_defect_routing_resets_retry_budget_for_previously_exhausted_worker(tmp_path: Path) -> None:
    """When a worker used all retries in initial dev, QA defect routing resets its budget to allow rework."""
    _write_worker_yaml(tmp_path, "WO-001", "gemini", "index.html")
    _write_worker_yaml(tmp_path, "WO-004", "gemma", "tests/test_animations.py")
    (tmp_path / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_animations.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    claude_inbox = tmp_path / ".sync" / "inbox" / "claude"
    claude_inbox.mkdir(parents=True, exist_ok=True)
    verdict = claude_inbox / "WO-004-qa-verdict.md"
    verdict.write_text(
        "# QA Verdict: WO-004\n## Status: NEEDS_CHANGES\n"
        "Blocker: index.html is missing element with id='animated-text'\n",
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001", "WO-004"])
    state.completed_wo_ids = ["WO-001"]
    # Simulate WO-001 having used all 2 retries in initial development
    state.retry_counts["WO-001"] = state.max_retries
    state.failed_wo_ids = ["WO-001"]

    wo4_op = mock_mgr.start_turn("sess-001", "QA page", work_order_id="WO-004", agent_id="gemma")
    mock_mgr.complete_operation(
        wo4_op["operation_id"],
        "COMPLETED",
        result={
            "status": "completed",
            "summary": "QA executed tests",
            "tool_calls_audit": [
                {"tool": "run_tests"},
                {"tool": "run_security_scan"},
            ],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" not in state.failed_wo_ids
    # Rework dispatch consumed 1 attempt of the reset fresh budget
    assert state.retry_counts["WO-001"] == 1
    # Verdict file should be moved to _consumed
    consumed = tmp_path / ".sync" / "reviews" / "_consumed"
    assert consumed.is_dir()
    assert any("WO-004-qa-verdict" in p.name for p in consumed.glob("*.md"))


def test_integration_rework_resets_retry_budget_for_reworkable_wos(tmp_path: Path) -> None:
    """Integration review blocker routing clears failed status, resets retry budget, and unarchives deliverable."""
    _write_worker_yaml(tmp_path, "WO-002", "codex", "src/app.py")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("SECRET_KEY = 'hardcoded'\n", encoding="utf-8")

    # Move WO-002 to completed
    active_wo = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml"
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)
    active_wo.rename(completed_dir / "WO-002.yaml")

    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-int", "Goal", tmp_path, "sess-int")
    state.phase = Phase.INTEGRATION_REVIEW
    state.worker_wo_ids = ["WO-002"]
    state.completed_wo_ids = ["WO-002"]
    state.max_retries = 2
    state.retry_counts["WO-002"] = 2
    state.failed_wo_ids = ["WO-002"]

    # Dispatch integration review operation
    int_op = mock_mgr.start_turn("sess-int", "Integration review", work_order_id="WO-REV", role="architect")
    state.integration_operation_id = int_op["operation_id"]
    state.integration_wo_id = "WO-REV"

    # Complete integration review as BLOCKED with blocker in src/app.py
    mock_mgr.complete_operation(
        int_op["operation_id"],
        "BLOCKED",
        result={
            "status": "blocked",
            "blockers": ["Hardcoded secret key in src/app.py"],
            "blocker_details": [
                {"finding": "Hardcoded secret key", "path": "src/app.py", "remediation": "Use env var"}
            ],
        },
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.integration_rework_rounds == 1
    assert "WO-002" not in state.completed_wo_ids
    assert "WO-002" not in state.failed_wo_ids
    assert state.retry_counts["WO-002"] == 0
    # Deliverable WO-002 unarchived to ACTIVE
    assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml").is_file()



