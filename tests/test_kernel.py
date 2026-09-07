"""Milestone A: the kernel is auditable without live-workspace mutation."""

from validators.kernel import (
    AgentSession, AuthorizationPolicy, ContractNormalizer, LifecycleState,
    OperationJournal, OperationRequest, OperationType, RuntimeBoundary,
)


def test_kernel_session_contract_operation_journal_without_workspace_mutation(tmp_path):
    workspace = tmp_path / "live-workspace"
    workspace.mkdir()
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")

    contract = ContractNormalizer.normalize({
        "agent": "codex", "wo": "WO-001", "contract_version": 3,
        "scope": {"allowed": ["scratch/**"], "denied": ["live-workspace/**"], "write": "read-write"},
    })
    session = AgentSession("codex", "test-provider", str(workspace), session_id="session-1")
    attempt = session.create_attempt(contract, attempt_id="attempt-1")
    session.transition(LifecycleState.RUNNING)
    attempt.transition(LifecycleState.RUNNING)

    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = AuthorizationPolicy.permit("human-approved", [OperationType.WRITE_FILE.value])
    request = OperationRequest(OperationType.WRITE_FILE, "live-workspace/blocked.txt", "session-1",
                               "attempt-1", "codex", "test-provider", correlation_id="parent-1")
    record = boundary.submit(request, attempt.contract, policy)

    assert not record.authorized
    assert record.reason == "target is explicitly denied"
    assert journal.records == (record,)
    assert journal.records[0].request.correlation_id == "parent-1"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (workspace / "blocked.txt").exists()


def test_attempt_contract_is_frozen_and_authorized_request_is_only_journaled():
    contract = ContractNormalizer.normalize({
        "agent_id": "codex", "work_order": "WO-001", "scope": {"allow": ["scratch/**"], "write": "read-write"},
    })
    attempt = AgentSession("codex", "provider", "workspace").create_attempt(contract)
    boundary = RuntimeBoundary(OperationJournal())
    record = boundary.submit(
        OperationRequest(OperationType.WRITE_FILE, "scratch/output.txt", "s", attempt.attempt_id, "codex", "provider"),
        attempt.contract,
        AuthorizationPolicy.permit("approved", ["write_file"]),
    )
    assert record.authorized
    assert attempt.contract is contract
    assert record.completed_at is None
