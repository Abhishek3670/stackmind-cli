"""P1 secure-execution gate tests."""

import sys

import pytest

from validators.kernel import (
    AuthorizationPolicy, ContractNormalizer, OperationJournal, RuntimeBoundary,
    ScratchWorkspace, ToolGateway, WorkspaceEscapeError,
)


def gateway(authoritative):
    (authoritative / "tracked.txt").write_text("live", encoding="utf-8")
    workspace = ScratchWorkspace.create(authoritative, "attempt-1")
    contract = ContractNormalizer.normalize({
        "agent": "codex", "wo": "WO-002",
        "scope": {"allow": ["workspace/**", "graph/**"], "deny": ["authoritative/**"], "write": "read-write"},
    })
    policy = AuthorizationPolicy.permit("human-approved", [
        "read_file", "write_file", "run_command", "query_graph",
    ])
    journal = OperationJournal()
    return workspace, ToolGateway(workspace, RuntimeBoundary(journal), contract, policy,
                                  "session-1", "attempt-1", "codex", "provider",
                                  graph_query=lambda query: {"query": query}), journal


def test_gateway_mutates_only_scratch_and_requires_verified_changeset(tmp_path):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir()
    workspace, tools, journal = gateway(authoritative)

    tools.write_file("result.txt", "scratch-only")
    assert (workspace.root / "result.txt").read_text(encoding="utf-8") == "scratch-only"
    assert not (authoritative / "result.txt").exists()
    assert (authoritative / "tracked.txt").read_text(encoding="utf-8") == "live"
    assert journal.records[-1].authorized and journal.records[-1].completed_at is not None
    with pytest.raises(PermissionError):
        workspace.change_set()
    workspace.verify()
    assert workspace.change_set() == workspace.root


def test_gateway_and_sandbox_fail_closed_on_escapes_and_journal_commands(tmp_path):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir()
    workspace, tools, journal = gateway(authoritative)

    with pytest.raises(PermissionError):
        tools.write_file("../authoritative/pwned.txt", "blocked")
    with pytest.raises(WorkspaceEscapeError):
        tools.run_command([sys.executable, "../escape"])
    assert not (authoritative / "pwned.txt").exists()
    assert journal.records[0].authorized is False
    assert journal.records[0].reason == "target path traversal is forbidden"
