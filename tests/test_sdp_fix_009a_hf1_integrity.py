"""SDP-FIX-009A-HF1 — cancellation outcome and authorization integrity tests.

DEFECT 1: a failed/timed-out cancellation request must never mark an operation
FAILED, and unknown outcomes must reconcile against the daemon before any new
operation is dispatched.

DEFECT 2: the `.sync/**` deny-rule exemption must require trusted Architecture
authorization plus supervisor provenance and must apply only to exact
`.sync/contracts/*.yaml` paths.

Deterministic mocks and failure injection only; no live provider is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from cli.tui.commands import (
    CANCEL_OUTCOME_UNKNOWN,
    CANCEL_REQUESTED,
    CancellationOutcome,
    _stream_turn_execution,
    apply_cancellation_outcome,
    dispatch_delivery_command,
    reconcile_pending_cancellations,
    resolve_cancellation_outcome,
    unresolved_cancellation_operations,
)
from cli.tui.state import AutonomousDeliveryState, OperationNode
from validators.harness import contract_validation
from validators.harness.contract_gate import verify_post_execution
from validators.harness.runner.models import HarnessDecision, HarnessTask
from validators.knowledge.contract import AgentContract, ContractAccessDenied


# =============================================================================
# DEFECT 1 — Cancellation outcome semantics
# =============================================================================

OP = "op-hf1"


def _interrupt_adapter() -> MagicMock:
    adapter = MagicMock()

    def _gen(*args, **kwargs):
        raise KeyboardInterrupt()
        yield  # pragma: no cover - makes this a generator

    adapter.stream = _gen
    return adapter


def _state_with_op(status: str = "RUNNING") -> AutonomousDeliveryState:
    state = AutonomousDeliveryState()
    state.operations[OP] = OperationNode(OP, "Turn", role="Architecture", status=status)
    return state


def _interrupt(
    *,
    cancel_return: object = None,
    cancel_side_effect: BaseException | None = None,
    get_return: object = None,
    get_side_effect: BaseException | None = None,
) -> tuple[AutonomousDeliveryState, MagicMock]:
    state = _state_with_op()
    session = {"session_id": "sess-hf1", "state": "RUNNING"}
    client = MagicMock()
    if cancel_side_effect is not None:
        client.operation_cancel.side_effect = cancel_side_effect
    else:
        client.operation_cancel.return_value = cancel_return
    if get_side_effect is not None:
        client.operation_get.side_effect = get_side_effect
    else:
        client.operation_get.return_value = get_return
    _stream_turn_execution(
        _interrupt_adapter(), client, session, state, op_id=OP, is_tty=False
    )
    return state, client


def test_cancellation_request_timeout_is_unknown_not_failed() -> None:
    state, _ = _interrupt(
        cancel_side_effect=TimeoutError("cancellation request timed out"),
        get_side_effect=TimeoutError("operation.get timed out"),
    )

    assert state.operations[OP].status == "RUNNING"
    record = state.cancel_outcomes[OP]
    assert record["outcome"] == CANCEL_OUTCOME_UNKNOWN
    assert record["authoritative_status"] == "RUNNING"
    msg = state.messages[-1].content.lower()
    assert "unknown" in msg
    assert "failed" not in msg


def test_connection_failure_before_daemon_acknowledgement() -> None:
    state, _ = _interrupt(
        cancel_side_effect=ConnectionError("daemon unreachable"),
        get_side_effect=ConnectionError("daemon unreachable"),
    )

    assert state.operations[OP].status == "RUNNING"
    assert state.cancel_outcomes[OP]["outcome"] == CANCEL_OUTCOME_UNKNOWN
    assert state.cancel_outcomes[OP]["confirmed"] is False
    assert state.cancel_outcomes[OP]["accepted"] is False


def test_connection_loss_after_daemon_acknowledgement_keeps_cancel_requested() -> None:
    state, client = _interrupt(cancel_return={"status": "CANCEL_REQUESTED"})

    assert state.cancel_outcomes[OP]["outcome"] == CANCEL_REQUESTED
    assert state.operations[OP].status == CANCEL_REQUESTED

    # Connection is lost before the daemon confirms termination.
    client.operation_get.side_effect = ConnectionError("connection lost")
    reconcile_pending_cancellations(client, state)

    assert state.cancel_outcomes[OP]["outcome"] == CANCEL_REQUESTED
    assert state.operations[OP].status == CANCEL_REQUESTED
    assert OP in unresolved_cancellation_operations(state)


def test_operation_still_running_after_failed_cancellation_request() -> None:
    state, _ = _interrupt(
        cancel_side_effect=RuntimeError("request failed"),
        get_return={"status": "RUNNING"},
    )

    assert state.operations[OP].status == "RUNNING"
    assert state.cancel_outcomes[OP]["outcome"] == "RUNNING"
    assert state.cancel_outcomes[OP]["reconciled"] is True
    msg = state.messages[-1].content
    assert "still RUNNING" in msg
    assert "failed" not in msg.lower()


def test_daemon_confirms_cancellation() -> None:
    state, _ = _interrupt(cancel_return={"status": "CANCELLED"})

    assert state.operations[OP].status == "CANCELLED"
    assert state.cancel_outcomes[OP]["outcome"] == "CANCELLED"
    assert state.cancel_outcomes[OP]["confirmed"] is True
    assert "cancelled by operator" in state.messages[-1].content


def test_daemon_confirms_execution_failure() -> None:
    state, _ = _interrupt(
        cancel_side_effect=RuntimeError("channel failed"),
        get_return={"status": "FAILED", "error": "provider crashed"},
    )

    assert state.operations[OP].status == "FAILED"
    assert state.cancel_outcomes[OP]["outcome"] == "FAILED"
    assert state.cancel_outcomes[OP]["confirmed"] is True
    assert "Daemon confirmed execution failure" in state.messages[-1].content


def test_daemon_confirms_successful_completion_is_preserved() -> None:
    state, _ = _interrupt(
        cancel_side_effect=RuntimeError("too late"),
        get_return={"status": "COMPLETED"},
    )

    assert state.operations[OP].status == "COMPLETED"
    assert state.cancel_outcomes[OP]["outcome"] == "COMPLETED"
    msg = state.messages[-1].content
    assert "completed before cancellation" in msg
    assert "cancelled by operator" not in msg


def test_restart_reconciliation_reconstructs_authoritative_state() -> None:
    state = _state_with_op(status="RUNNING")
    state.cancel_outcomes[OP] = CancellationOutcome(
        operation_id=OP,
        outcome=CANCEL_OUTCOME_UNKNOWN,
        authoritative_status="RUNNING",
    ).to_dict()

    client = MagicMock()
    client.operation_get.return_value = {"status": "CANCELLED"}

    resolved = reconcile_pending_cancellations(client, state)

    assert len(resolved) == 1
    assert resolved[0].reconciled is True
    assert state.operations[OP].status == "CANCELLED"
    assert state.cancel_outcomes[OP]["outcome"] == "CANCELLED"


def test_restart_reconciliation_preserves_running_when_daemon_unreachable() -> None:
    state = _state_with_op(status="RUNNING")
    state.cancel_outcomes[OP] = CancellationOutcome(
        operation_id=OP,
        outcome=CANCEL_OUTCOME_UNKNOWN,
        authoritative_status="RUNNING",
    ).to_dict()

    client = MagicMock()
    client.operation_get.side_effect = ConnectionError("offline")

    reconcile_pending_cancellations(client, state)

    assert state.operations[OP].status == "RUNNING"
    assert state.cancel_outcomes[OP]["outcome"] == CANCEL_OUTCOME_UNKNOWN


def test_repeated_cancellation_requests_preserve_accounting_and_escalate_safely() -> None:
    state, _ = _interrupt(cancel_return={"status": "CANCEL_REQUESTED"})
    state.record_attempt(attempt=1, retry=0, max_attempts=10, max_retries=5)

    # A repeated request that the daemon confirms must converge on CANCELLED.
    outcome = resolve_cancellation_outcome(
        MagicMock(), state, OP, cancel_resp={"status": "CANCELLED"}
    )
    apply_cancellation_outcome(state, outcome)

    assert state.operations[OP].status == "CANCELLED"
    assert state.cancel_outcomes[OP]["outcome"] == "CANCELLED"
    # Attempt accounting and operation identity are untouched.
    assert state.attempt_count == 1
    assert state.operations[OP].operation_id == OP


def test_no_duplicate_worker_dispatch_while_outcome_unknown() -> None:
    state = _state_with_op(status="RUNNING")
    state.cancel_outcomes[OP] = CancellationOutcome(
        operation_id=OP,
        outcome=CANCEL_OUTCOME_UNKNOWN,
        authoritative_status="RUNNING",
    ).to_dict()

    session = {"session_id": "sess-hf1", "state": "RUNNING"}
    adapter = MagicMock()
    client = MagicMock()
    client.operation_get.side_effect = ConnectionError("daemon down")
    client.get_session.side_effect = ConnectionError("daemon down")

    _, should_exit = dispatch_delivery_command(
        adapter, client, session, "keep working on the feature", state, client_timeout=5
    )

    assert should_exit is False
    adapter.command.assert_not_called()
    system_msgs = [m.content for m in state.messages if m.role == "system"]
    assert any("no duplicate turn was dispatched" in m for m in system_msgs)


def test_unknown_outcome_does_not_create_an_operation_node() -> None:
    state = AutonomousDeliveryState()
    client = MagicMock()
    client.operation_get.side_effect = ConnectionError("offline")

    outcome = resolve_cancellation_outcome(
        client, state, "op-absent", cancel_err=ConnectionError("offline")
    )
    apply_cancellation_outcome(state, outcome)

    assert outcome.outcome == CANCEL_OUTCOME_UNKNOWN
    assert "op-absent" not in state.operations


# =============================================================================
# DEFECT 2 — Scoped `.sync/**` deny-rule exemption
# =============================================================================

WO = "WO-001"


def _task(is_authoring: bool = True, work_order: str = WO) -> HarnessTask:
    return HarnessTask(
        kind="work_order",
        identifier=work_order,
        path=Path(f"{work_order}.yaml"),
        title="Repair contract",
        body="Repair",
        query="",
        work_order_id=work_order,
        is_authoring=is_authoring,
    )


def _decision(*files: str) -> HarnessDecision:
    return HarnessDecision(
        status="completed",
        summary="Repaired contract",
        report_markdown="Done",
        blockers=(),
        modified_files=tuple(files),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
    )


def _write_contract(
    tmp_path: Path,
    *,
    agent_id: str = "claude",
    work_order: str = WO,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    identity_role: str | None = None,
) -> Path:
    data = {
        "schema_version": 1,
        "agent_id": agent_id,
        "work_order": work_order,
        "scope": {
            "allow": [{"module": m} for m in (allow if allow is not None else [".sync/contracts/**"])],
            "deny": [{"module": m} for m in (deny if deny is not None else [".sync/contracts/**"])],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 10, "max_tokens": 50000},
    }
    if identity_role:
        data["identity"] = {"role": identity_role}
    contracts = tmp_path / ".sync" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    path = contracts / f"{work_order}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _write_provenance(
    tmp_path: Path,
    *,
    work_order: str = WO,
    action: str = "amend_contract",
    payload: dict | None = None,
) -> Path:
    decisions = tmp_path / ".sync" / "decisions" / "recovery"
    decisions.mkdir(parents=True, exist_ok=True)
    path = decisions / f"{work_order}.decision.json"
    data = payload if payload is not None else {
        "work_order": work_order,
        "action": action,
        "reason": "authorize contract repair",
        "failure_code": "OUTCOME_NOT_VERIFIED",
        "contract_revision": 2,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_authorized_architecture_repair_can_modify_contract_yaml(tmp_path: Path) -> None:
    _write_contract(tmp_path, allow=[".sync/contracts/**"], deny=[".sync/contracts/**"])
    _write_provenance(tmp_path)

    # Must not raise.
    verify_post_execution(
        project_path=tmp_path,
        agent="claude",
        task=_task(),
        decision=_decision(".sync/contracts/WO-001.yaml"),
        observed_files=(".sync/contracts/WO-001.yaml",),
    )


def test_authorized_repair_also_passes_the_contract_validation_gate(tmp_path: Path) -> None:
    contract_path = _write_contract(
        tmp_path, allow=[".sync/contracts/**"], deny=[".sync/contracts/**"]
    )
    _write_provenance(tmp_path)
    contract = AgentContract.load(contract_path, tmp_path)

    contract_validation.verify_post_execution(
        project_path=tmp_path,
        agent="claude",
        task=_task(),
        decision=_decision(".sync/contracts/WO-001.yaml"),
        observed_files=(".sync/contracts/WO-001.yaml",),
        contract=contract,
    )


@pytest.mark.parametrize(
    "target",
    [
        ".sync/policies/security.yaml",
        ".sync/governance/rules.yaml",
        ".sync/work-orders/ACTIVE/WO-002.yaml",
        ".sync/INDEX.yaml",
    ],
)
def test_architecture_cannot_modify_unrelated_sync_paths(tmp_path: Path, target: str) -> None:
    _write_contract(tmp_path, allow=[".sync/**"], deny=[".sync/**"])
    _write_provenance(tmp_path)

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(),
            decision=_decision(target),
            observed_files=(target,),
        )


@pytest.mark.parametrize("worker", ["codex", "gemini", "local-llm", "gitops"])
def test_workers_remain_denied_even_with_authoring_and_provenance(
    tmp_path: Path, worker: str
) -> None:
    _write_contract(tmp_path, agent_id=worker, allow=["src/**"], deny=[".sync/**"])
    _write_provenance(tmp_path)

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent=worker,
            task=_task(),
            decision=_decision(".sync/contracts/WO-001.yaml"),
            observed_files=(".sync/contracts/WO-001.yaml",),
        )


def test_worker_cannot_impersonate_architecture_through_string_fields(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        agent_id="codex",
        allow=["src/**"],
        deny=[".sync/**"],
        identity_role="architecture",
    )
    _write_provenance(tmp_path)

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="codex",
            task=_task(),
            decision=_decision(".sync/contracts/WO-001.yaml"),
            observed_files=(".sync/contracts/WO-001.yaml",),
        )


def test_authoring_flag_alone_cannot_grant_exemption(tmp_path: Path) -> None:
    _write_contract(tmp_path, allow=[".sync/contracts/**"], deny=[".sync/contracts/**"])
    # No recovery decision provenance exists.
    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(is_authoring=True),
            decision=_decision(".sync/contracts/WO-001.yaml"),
            observed_files=(".sync/contracts/WO-001.yaml",),
        )


@pytest.mark.parametrize(
    "payload",
    [
        None,  # malformed JSON handled separately
        {"work_order": "WO-002", "action": "amend_contract"},
        {"work_order": WO, "action": "escalate_human"},
        {"work_order": WO, "action": "terminal_block"},
    ],
)
def test_missing_or_invalid_provenance_fails_closed(tmp_path: Path, payload) -> None:
    _write_contract(tmp_path, allow=[".sync/contracts/**"], deny=[".sync/contracts/**"])
    decisions = tmp_path / ".sync" / "decisions" / "recovery"
    decisions.mkdir(parents=True, exist_ok=True)
    path = decisions / f"{WO}.decision.json"
    if payload is None:
        path.write_text("{not-json", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(),
            decision=_decision(".sync/contracts/WO-001.yaml"),
            observed_files=(".sync/contracts/WO-001.yaml",),
        )


def test_non_authoring_task_cannot_use_exemption(tmp_path: Path) -> None:
    _write_contract(tmp_path, allow=[".sync/contracts/**"], deny=[".sync/contracts/**"])
    _write_provenance(tmp_path)

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(is_authoring=False),
            decision=_decision(".sync/contracts/WO-001.yaml"),
            observed_files=(".sync/contracts/WO-001.yaml",),
        )


@pytest.mark.parametrize(
    "target",
    [
        ".sync/contracts/../policies/security.yaml",
        ".sync/contracts/./WO-001.yaml",
        ".sync//contracts/WO-001.yaml",
        ".sync/contracts/nested/WO-001.yaml",
        "/.sync/contracts/../work-orders/ACTIVE/WO-001.yaml",
    ],
)
def test_path_traversal_and_normalization_cannot_escape_contracts(
    tmp_path: Path, target: str
) -> None:
    _write_contract(tmp_path, allow=[".sync/**"], deny=[".sync/**"])
    _write_provenance(tmp_path)

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(),
            decision=_decision(target),
            observed_files=(target,),
        )


def test_existing_deny_rules_remain_effective_outside_exemption(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        allow=[".sync/**"],
        deny=[".sync/**", "src/secrets/**"],
    )
    _write_provenance(tmp_path)

    # The permitted contract path is allowed...
    verify_post_execution(
        project_path=tmp_path,
        agent="claude",
        task=_task(),
        decision=_decision(".sync/contracts/WO-001.yaml"),
        observed_files=(".sync/contracts/WO-001.yaml",),
    )

    # ... but an unrelated deny rule outside the exemption still fires.
    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path,
            agent="claude",
            task=_task(),
            decision=_decision("src/secrets/key.py"),
            observed_files=("src/secrets/key.py",),
        )
