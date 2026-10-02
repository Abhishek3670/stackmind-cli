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


def test_proposed_capability_matrix_role_policies():
    """Verify role policies strictly adhere to stackmind-proposed-agent-capability-matrix.md."""
    from validators.kernel.identity import get_role_policy
    from validators.kernel.operations import OperationType, is_mutating_operation

    # 1. Claude (Architecture)
    claude_policy = get_role_policy("claude")
    assert claude_policy.permits(OperationType.CREATE_WORK_ORDER.value)
    assert claude_policy.permits(OperationType.ENTER_PLAN_MODE.value)
    assert claude_policy.permits(OperationType.SKILL_MINE.value)
    assert claude_policy.permits(OperationType.ASK_USER.value)
    # Architecture is strictly forbidden from implementation mutations
    assert not claude_policy.permits(OperationType.APPLY_PATCH.value)
    assert not claude_policy.permits(OperationType.GIT_COMMIT.value)
    assert not claude_policy.permits(OperationType.CREATE_RELEASE.value)

    # 2. Codex (Backend)
    codex_policy = get_role_policy("codex")
    assert codex_policy.permits(OperationType.APPLY_PATCH.value)
    assert codex_policy.permits(OperationType.WRITE_FILE.value)
    assert codex_policy.permits(OperationType.RUN_TESTS.value)
    assert codex_policy.permits(OperationType.PROCESS_START.value)
    # Backend cannot author contracts, work orders, or commit/release
    assert not codex_policy.permits(OperationType.CREATE_WORK_ORDER.value)
    assert not codex_policy.permits(OperationType.GIT_COMMIT.value)
    assert not codex_policy.permits(OperationType.CREATE_RELEASE.value)

    # 3. Gemini (Frontend)
    gemini_policy = get_role_policy("gemini")
    assert gemini_policy.permits(OperationType.BROWSER_OPEN.value)
    assert gemini_policy.permits(OperationType.BROWSER_SCREENSHOT.value)
    assert gemini_policy.permits(OperationType.APPLY_PATCH.value)
    assert not gemini_policy.permits(OperationType.GIT_COMMIT.value)
    assert not gemini_policy.permits(OperationType.CREATE_WORK_ORDER.value)

    # 4. Gemma (QA Lead)
    gemma_policy = get_role_policy("gemma")
    assert gemma_policy.permits(OperationType.VERIFY_DELIVERABLE.value)
    assert gemma_policy.permits(OperationType.SUBMIT_VERDICT.value)
    assert gemma_policy.permits(OperationType.REQUEST_CHANGES.value)
    assert gemma_policy.permits(OperationType.RUN_TESTS.value)
    assert gemma_policy.permits(OperationType.SKILL_TEST.value)
    # QA is an evaluator: file mutations are withheld
    assert not gemma_policy.permits(OperationType.APPLY_PATCH.value)
    assert not gemma_policy.permits(OperationType.WRITE_FILE.value)
    assert not gemma_policy.permits(OperationType.GIT_COMMIT.value)

    # 5. Local-LLM (GitOps)
    local_policy = get_role_policy("local-llm")
    assert local_policy.permits(OperationType.GIT_COMMIT.value)
    assert local_policy.permits(OperationType.GIT_PUSH.value)
    assert local_policy.permits(OperationType.CREATE_RELEASE.value)
    assert local_policy.permits(OperationType.UPDATE_VERSION.value)
    assert local_policy.permits(OperationType.UPDATE_CHANGELOG.value)
    # Local-LLM cannot author work orders or change architecture
    assert not local_policy.permits(OperationType.CREATE_WORK_ORDER.value)
    assert not local_policy.permits(OperationType.ENTER_PLAN_MODE.value)

    # 6. Mutation classification
    assert is_mutating_operation(OperationType.WRITE_FILE)
    assert is_mutating_operation(OperationType.APPLY_PATCH)
    assert is_mutating_operation(OperationType.GIT_COMMIT)
    assert not is_mutating_operation(OperationType.READ_FILE)
    assert not is_mutating_operation(OperationType.GIT_STATUS)
    assert not is_mutating_operation(OperationType.QUERY_GRAPH)
