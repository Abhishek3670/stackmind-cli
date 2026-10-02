"""Tests for Phase 6 GitOps Isolation and Release Tools in ToolGateway."""

import subprocess
from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "local-llm", allow_patterns=None, deny_patterns=None, write_mode="read-write"):
    if allow_patterns is None:
        allow_patterns = ["**"]
    if deny_patterns is None:
        deny_patterns = []

    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": allow_patterns,
            "denied": deny_patterns,
            "write": write_mode,
        },
    })
    session = AgentSession(agent, "test-provider", str(tmp_path), session_id="s1")
    attempt = session.create_attempt(contract, attempt_id="a1")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = get_role_policy(agent)
    workspace = ScratchWorkspace(authoritative_root=tmp_path, root=tmp_path, attempt_id="a1")
    gateway = ToolGateway(
        boundary=boundary,
        policy=policy,
        contract=attempt.contract,
        workspace=workspace,
        sandbox=None,
        session_id="s1",
        attempt_id="a1",
        actor_id=agent,
        provider_id="test-provider",
    )
    return gateway, journal


def _init_git_repo(path: Path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agent@stackmind.test"], cwd=path, capture_output=True, check=True)


def test_git_inspection_allowed_for_all_agents(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    # Claude inspects status
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    status = claude_gw.git_status()
    assert "hello.txt" in status["untracked"]
    assert status["clean"] is False

    # Codex inspects branch
    codex_gw, _ = _setup_gateway(tmp_path, "codex")
    branch = codex_gw.git_branch()
    assert "current" in branch


def test_role_enforcement_git_mutations_forbidden_for_workers_and_architect(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "mod.txt").write_text("mod", encoding="utf-8")

    # 1. Claude cannot stage or commit
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.git_stage(["mod.txt"])

    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.git_commit("fix: test")

    # 2. Codex cannot stage or commit
    codex_gw, _ = _setup_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.git_stage(["mod.txt"])

    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.git_commit("fix: test")

    # 3. Gemma cannot commit or release
    gemma_gw, _ = _setup_gateway(tmp_path, "gemma")
    with pytest.raises(PermissionError, match="policy denies operation"):
        gemma_gw.git_commit("fix: test")

    with pytest.raises(PermissionError, match="policy denies operation"):
        gemma_gw.create_release("v1.0.0", "notes")


def test_local_llm_git_mutation_and_release_lifecycle(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

    llm_gw, journal = _setup_gateway(tmp_path, "local-llm")

    # 1. Stage file
    staged = llm_gw.git_stage(["code.py"])
    assert "code.py" in staged

    # 2. Commit file
    commit_sha = llm_gw.git_commit("feat: initial commit")
    assert commit_sha != "unknown"
    assert len(commit_sha) >= 7

    # 3. Tag
    tag_name = llm_gw.git_tag("v0.1.0", "Release 0.1.0")
    assert tag_name == "v0.1.0"

    # 4. Release creation with changelog
    rel = llm_gw.create_release("v0.2.0", "- Added feature X\n- Fixed bug Y")
    assert rel["tag"] == "v0.2.0"
    assert (tmp_path / "CHANGELOG.md").is_file()
    assert "Added feature X" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")

    # 5. Rollback release
    rb = llm_gw.rollback_release("v0.2.0")
    assert rb["status"] == "release_rolled_back"
