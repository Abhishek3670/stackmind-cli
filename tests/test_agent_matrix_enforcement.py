"""Enforcement tests for docs/AGENT_MATRIX.md — 5-Agent Responsibility & Access Matrix.

Two layers are verified:

1. Policy conformance: the role catalogs in validators/kernel/identity.py
   (get_role_policy) must match the matrix's authority rows — git mutation is
   exclusive to local-llm, QA verdicts exclusive to gemma, work-order/contract
   authoring exclusive to claude, browser to gemini/gemma, and the shared
   read-only knowledge layer for everyone (INV-001..INV-006, INV-010).

2. Verdict-channel write gate (INV-005, "Self-Approval: ❌"): implementation
   workers cannot author QA verdict/review artifacts in the channels the
   supervisor's D024 gate reads — only gemma (verdict authority) and claude
   (governance authority) may.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import ContractNormalizer
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationJournal, OperationType
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace

AGENTS = ("claude", "codex", "gemini", "gemma", "local-llm")


def _permits(agent: str, *ops: str) -> bool:
    policy = get_role_policy(agent)
    return all(policy.permits(op) for op in ops)


# ─── 1. Policy catalog conformance (Agent Matrix §2, §10, §11) ────────────


def test_git_mutation_is_exclusive_to_local_llm() -> None:
    git_mutation = (
        OperationType.GIT_STAGE.value,
        OperationType.GIT_COMMIT.value,
        OperationType.GIT_TAG.value,
        OperationType.GIT_PUSH.value,
        OperationType.CREATE_RELEASE.value,
        OperationType.ROLLBACK_RELEASE.value,
    )
    for op in git_mutation:
        assert _permits("local-llm", op), f"local-llm must hold {op} (INV-006)"
        for other in AGENTS:
            if other != "local-llm":
                assert not get_role_policy(other).permits(op), (
                    f"{other} must not hold {op}: git mutation is centralized"
                )


def test_qa_verdict_authority_is_exclusive_to_gemma() -> None:
    verdict_ops = (
        OperationType.SUBMIT_VERDICT.value,
        OperationType.REQUEST_CHANGES.value,
        OperationType.APPROVE_WORK_ORDER.value,
    )
    for op in verdict_ops:
        assert _permits("gemma", op), f"gemma must hold {op} (INV-005)"
        for other in AGENTS:
            if other != "gemma":
                assert not get_role_policy(other).permits(op), (
                    f"{other} must not hold {op}: QA verdict is gemma's authority"
                )


def test_work_order_authoring_is_exclusive_to_claude() -> None:
    authoring_ops = (
        OperationType.CREATE_WORK_ORDER.value,
        OperationType.UPDATE_WORK_ORDER.value,
        OperationType.DISPATCH_SUBAGENT.value,
    )
    for op in authoring_ops:
        assert _permits("claude", op), f"claude must hold {op}"
        for other in AGENTS:
            if other != "claude":
                assert not get_role_policy(other).permits(op), (
                    f"{other} must not hold {op}: work-order authoring is the architect's authority"
                )


def test_browser_access_follows_the_matrix() -> None:
    browser_ops = (
        OperationType.BROWSER_OPEN.value,
        OperationType.BROWSER_NAVIGATE.value,
        OperationType.BROWSER_CLICK.value,
        OperationType.BROWSER_TYPE.value,
        OperationType.BROWSER_SCREENSHOT.value,
    )
    for op in browser_ops:
        assert _permits("gemini", op) and _permits("gemma", op), f"{op} belongs to gemini/gemma"
        for other in ("claude", "codex", "local-llm"):
            assert not get_role_policy(other).permits(op), f"{other} must not hold {op}"


def test_source_editing_and_test_tools_follow_the_matrix() -> None:
    # Refactoring tools: codex/gemini/local-llm; withheld from claude and gemma.
    for agent in ("codex", "gemini", "local-llm"):
        assert _permits(agent, "apply_patch")
    for agent in ("claude", "gemma"):
        assert not get_role_policy(agent).permits("apply_patch")
    # Test execution and security scanning: workers + QA, not the architect.
    for agent in ("codex", "gemini", "gemma", "local-llm"):
        assert _permits(agent, "run_tests", "run_security_scan")
    assert not get_role_policy("claude").permits("run_tests")
    # Gemma edits tests but never refactors application code (INV-007).
    assert _permits("gemma", "write_file")
    assert not get_role_policy("gemma").permits("delete_file")


def test_knowledge_layer_is_shared_read_only() -> None:
    shared = (
        "query_graph", "get_context", "semantic_search", "knowledge_stats",
        "find_callers", "find_callees", "impact_analysis", "dependency_analysis",
        "read_file", "read_many_files", "list_directory", "glob", "grep",
        "find_symbol", "find_references", "git_log", "git_diff", "git_status",
        "get_contract", "verify_scope",
    )
    for agent in AGENTS:
        assert _permits(agent, *shared), f"{agent} must hold the shared knowledge layer (INV-001)"


# ─── 2. Verdict-channel write gate (INV-005, §12 .sync/inbox RW*) ─────────


def _make_gateway(tmp_path: Path, agent: str) -> ToolGateway:
    authoritative = tmp_path / "project"
    ws_dir = tmp_path / "scratch"
    (authoritative / ".sync" / "inbox" / "claude").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "reviews").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "qa" / "verdicts").mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)
    contract = ContractNormalizer.normalize({
        "agent_id": agent,
        "work_order": "WO-001",
        "scope": {"allow": ["*"], "deny": [], "write": "read-write"},
        "budget": {"max_tokens": 10000, "max_tool_calls": 10, "max_turns": 5},
    })
    return ToolGateway(
        workspace=ScratchWorkspace(authoritative_root=authoritative, root=ws_dir, attempt_id="attempt-1"),
        boundary=RuntimeBoundary(OperationJournal()),
        contract=contract,
        policy=get_role_policy(agent),
        session_id="session-1",
        attempt_id="attempt-1",
        actor_id=agent,
        provider_id="mock",
    )


def test_worker_cannot_author_qa_verdict_in_architect_inbox(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="Verdict authority denied"):
        gateway.write_file(".sync/inbox/claude/WO-001-qa-verdict.md", "Verdict: APPROVED")


def test_worker_cannot_author_review_files(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "gemini")
    with pytest.raises(PermissionError, match="Verdict authority denied"):
        gateway.write_file(".sync/reviews/WO-001-review.md", "Verdict: APPROVED")


def test_worker_cannot_write_plain_files_to_qa_inbox_channel(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="Verdict authority denied"):
        gateway.write_file(".sync/inbox/claude/answer.md", "not a verdict")


def test_qa_lead_can_write_verdicts(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "gemma")
    gateway.write_file(
        ".sync/inbox/claude/WO-001-qa-verdict.md",
        "Verdict: NEEDS_CHANGES\nindex.html defect",
    )
    assert (gateway.workspace.root / ".sync" / "inbox" / "claude" / "WO-001-qa-verdict.md").is_file()


def test_architect_can_write_integration_review_reports(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "claude")
    gateway.write_file(
        ".sync/reviews/WO-005-integration-review.md",
        "Integration review: PASS with notes",
    )
    assert (gateway.workspace.root / ".sync" / "reviews" / "WO-005-integration-review.md").is_file()


def test_worker_outbox_and_unrelated_reviews_writes_remain_allowed(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "codex")
    gateway.write_file(".sync/outbox/codex/notes.md", "findings for the architect")
    # Reviews entries without verdict/review naming stay open (lenient rule).
    gateway.write_file(".sync/reviews/worker-scratch-notes.txt", "worklog")
    assert (gateway.workspace.root / ".sync" / "outbox" / "codex" / "notes.md").is_file()


def test_gitops_lead_cannot_author_verdicts(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "local-llm")
    with pytest.raises(PermissionError, match="Verdict authority denied"):
        gateway.write_file(".sync/qa/verdicts/WO-001.md", "Verdict: APPROVED")


def test_verdict_channel_denied_via_apply_patch_hunks(tmp_path: Path) -> None:
    gateway = _make_gateway(tmp_path, "codex")
    (gateway.workspace.root / "tests").mkdir(exist_ok=True)
    (gateway.workspace.root / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    patch = (
        "--- a/tests/test_x.py\n"
        "+++ b/.sync/inbox/claude/WO-001-qa-verdict.md\n"
        "@@ -1 +0,0 @@\n"
        "-x = 1\n"
    )
    with pytest.raises(PermissionError, match="Verdict authority denied"):
        gateway.apply_patch("tests/test_x.py", patch)
