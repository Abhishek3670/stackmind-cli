"""Architect visibility + task-amendment regression tests.

Covers the three levers that let the Architect improve a peer worker's task:

1. `render_work_order_board` — peers' tasks (owner, deliverable, acceptance
   criteria, last amendment) reach the Architect's turn as evidence.
2. Revision-gated amendment — an authorized author may rewrite a live ACTIVE
   work order only behind a strictly increasing revision; workers never can.
3. `update_work_order` — amendments are recorded as an audit trail and the
   compiled worker contract / prompt surface them on the worker's next turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.task_contract import (
    compile_worker_task_contract,
    render_task_block,
    render_worker_system,
    render_work_order_board,
)
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


def _wo(wo_id: str, agent: str, path: str, **extra) -> dict:
    record = {
        "id": wo_id,
        "type": "FEATURE",
        "title": f"Deliver {path}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": [],
        "deliverable": {"type": "code", "path": path, "description": f"Author {path}"},
        "acceptance_criteria": [f"{path} exists", f"{path} integrates"],
    }
    record.update(extra)
    return record


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    return root


def _write_wo(root: Path, data: dict, bucket: str = "ACTIVE") -> Path:
    target = root / ".sync" / "work-orders" / bucket / f"{data['id']}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


# ─── 1. Board visibility ─────────────────────────────────────────────────


def test_board_renders_peer_tasks_with_acceptance_criteria(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py"))
    _write_wo(root, _wo(
        "WO-002",
        "gemini",
        "index.html",
        revision=2,
        amendments=[{
            "revision": 2,
            "agent": "claude",
            "reason": "Add the #welcome-banner hook the script queries.",
            "changed_fields": ["acceptance_criteria"],
        }],
    ))

    board = render_work_order_board(root)

    assert "WO-001" in board and "WO-002" in board
    assert "owner=codex" in board and "owner=gemini" in board
    assert "deliverable=index.html" in board
    assert "index.html exists" in board
    assert "revision=2" in board
    assert "Add the #welcome-banner hook" in board


def test_board_excludes_own_work_order_and_returns_empty_without_peers(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py"))

    assert render_work_order_board(root, exclude_wo="WO-001") == ""
    assert render_work_order_board(tmp_path / "nothing-here") == ""


def test_architect_turn_receives_the_peer_board(tmp_path: Path):
    """End-to-end: the board actually reaches the Architect's model input."""
    from validators.harness.runner import AgentRunner

    root = tmp_path / "arch"
    (root / ".sync" / "inbox" / "claude").mkdir(parents=True)
    (root / ".sync" / "runtime").mkdir(parents=True)
    (root / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    (root / ".sync" / "runtime" / "TREE.yaml").write_text(
        yaml.safe_dump({"agents": {}, "work_orders": {"total_active": 1}}), encoding="utf-8",
    )
    _write_wo(root, _wo(
        "WO-004",
        "gemini",
        "index.html",
        acceptance_criteria=["#animated-name renders with the requested text"],
    ))

    class _Adapter:
        provider_name = "mock"
        model_name = "mock-model"

        def __init__(self) -> None:
            self.received: list = []

        def complete(self, messages, cancellation_token=None):
            self.received = list(messages)
            from dataclasses import dataclass

            @dataclass
            class _Msg:
                content: str

            @dataclass
            class _Resp:
                message: _Msg
                usage: object = None

            return _Resp(message=_Msg(content="Noted."))

    adapter = _Adapter()
    runner = AgentRunner(root, "claude")
    runner.provider_adapter = adapter
    runner.run_once(prompt="what is the frontend working on?")

    prompt_text = "\n".join(str(m.content) for m in adapter.received)
    assert "SIBLING WORK ORDER BOARD" in prompt_text
    assert "WO-004" in prompt_text
    assert "#animated-name renders with the requested text" in prompt_text


def test_board_skips_planning_work_order(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-000", "claude", "PLAN.md"))
    _write_wo(root, _wo("WO-003", "gemma", "tests/test_api.py"))

    board = render_work_order_board(root)

    assert "WO-000" not in board
    assert "WO-003" in board


# ─── 2. Revision-gated amendment (a fresh gate = a new turn) ─────────────


def test_fresh_gate_allows_amend_with_incremented_revision(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py"))

    amended = _wo(
        "WO-001",
        "codex",
        "src/api.py",
        revision=2,
        acceptance_criteria=["src/api.py exists", "Returns 404 for unknown ids"],
        amended_by="claude",
        amendments=[{
            "revision": 2,
            "agent": "claude",
            "reason": "Spell out the 404 contract.",
            "changed_fields": ["acceptance_criteria"],
        }],
    )

    decision = AuthoringGate(project_root=root).validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(amended, sort_keys=False),
        agent="claude",
    )

    assert decision.passed, decision.errors


def test_amendment_without_revision_bump_is_still_rejected(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py", revision=2))

    silent_rewrite = _wo("WO-001", "codex", "src/api.py", revision=2, title="Something else")

    decision = AuthoringGate(project_root=root).validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(silent_rewrite, sort_keys=False),
        agent="claude",
    )

    assert not decision.passed
    assert any("Overwrite conflict" in err for err in decision.errors)


def test_worker_cannot_amend_even_with_higher_revision(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py"))

    worker_edit = _wo("WO-001", "codex", "src/api.py", revision=2, title="Worker rewrite")

    decision = AuthoringGate(project_root=root).validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(worker_edit, sort_keys=False),
        agent="codex",
    )

    assert not decision.passed
    assert any("not authorized" in err for err in decision.errors)


# ─── 3. Amendment is recorded and reaches the worker ─────────────────────


def _gateway(root: Path) -> ToolGateway:
    workspace = ScratchWorkspace.create(root, attempt_id="test-amendment")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-000",
        allow=("workspace/.sync/work-orders/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit(
        "architect",
        ("update_work_order", "write_file", "read_file", "inspect_agent"),
    )
    return ToolGateway(
        workspace=workspace,
        boundary=RuntimeBoundary(OperationJournal()),
        contract=contract,
        policy=policy,
        session_id="s1",
        attempt_id="a1",
        actor_id="claude",
        provider_id="mock",
    )


def test_update_work_order_bumps_revision_and_appends_audit_trail(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py", revision=1))

    gateway = _gateway(root)
    updated = gateway.update_work_order(
        "WO-001",
        {"acceptance_criteria": ["src/api.py exists", "Returns 404 for unknown ids"]},
        reason="Spell out the 404 contract.",
        expected_revision=1,
    )

    assert updated["revision"] == 2
    assert updated["amended_by"] == "claude"
    assert updated["amendments"][-1]["revision"] == 2
    assert updated["amendments"][-1]["reason"] == "Spell out the 404 contract."
    assert "acceptance_criteria" in updated["amendments"][-1]["changed_fields"]

    # Amendments are canonical state: they persist to the authoritative root.
    on_disk_path = root / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
    on_disk = yaml.safe_load(on_disk_path.read_text(encoding="utf-8"))
    assert on_disk["revision"] == 2
    assert on_disk["amendments"][-1]["agent"] == "claude"


def test_inspect_agent_reports_assignments_through_role_alias(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-007", "gemini", "index.html"))
    _write_wo(root, _wo("WO-008", "codex", "src/api.py"))

    info = _gateway(root).inspect_agent("frontend")

    assigned = info["assigned_work_orders"]
    assert [wo["id"] for wo in assigned] == ["WO-007"]
    assert assigned[0]["deliverable"] == "index.html"
    assert assigned[0]["acceptance_criteria"] == ["index.html exists", "index.html integrates"]


def test_architect_investigation_palette_exposes_amendment_tool():
    """The conversational Architect can amend a peer task; the authoring
    palette stays at its deliberate 4-tool budget (it amends via write_file
    with an incremented revision)."""
    from validators.kernel.providers.gateway import get_curated_tools_for_phase

    investigation = {t.name for t in get_curated_tools_for_phase("claude")}
    authoring = get_curated_tools_for_phase("claude", phase="authoring")

    assert "update_work_order" in investigation
    assert len(authoring) <= 4


def test_amendment_written_by_gateway_reaches_the_worker_prompt(tmp_path: Path):
    """Full loop: Architect amends a peer WO -> worker's next turn is briefed."""
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", "codex", "src/api.py", revision=1))

    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001",
        {"acceptance_criteria": ["src/api.py exists", "Returns 404 for unknown ids"]},
        reason="Spell out the 404 contract.",
        expected_revision=1,
    )
    stored = yaml.safe_load(
        (root / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text(encoding="utf-8")
    )

    class _Task:
        work_order_id = "WO-001"
        deliverable_path = "src/api.py"
        title = "Deliver src/api.py"
        body = ""

    contract = compile_worker_task_contract(_Task(), stored, None, root)
    system_msg = render_worker_system(contract)

    assert contract["acceptance_criteria"] == [
        "src/api.py exists", "Returns 404 for unknown ids",
    ]
    assert "Spell out the 404 contract." in system_msg
    assert contract["architect_amendments"][-1]["revision"] == 2


def test_compiled_contract_and_prompt_carry_architect_amendment(tmp_path: Path):
    wo_data = _wo(
        "WO-001",
        "codex",
        "src/api.py",
        revision=2,
        amendments=[{
            "revision": 2,
            "agent": "claude",
            "reason": "Spell out the 404 contract.",
            "changed_fields": ["acceptance_criteria"],
        }],
    )

    class _Task:
        work_order_id = "WO-001"
        deliverable_path = "src/api.py"
        title = "Deliver src/api.py"
        body = ""

    contract = compile_worker_task_contract(_Task(), wo_data, None, tmp_path)

    assert contract["revision"] == 2
    assert contract["architect_amendments"][-1]["reason"] == "Spell out the 404 contract."

    system_msg = render_worker_system(contract)
    assert "The Architect amended this work order" in system_msg
    assert "Spell out the 404 contract." in system_msg

    block = render_task_block(contract)
    assert "architect_amendments" in block
