"""Tests for Phase 2 ToolGateway extensions: Discovery and Patch Editing."""

import pytest
from pathlib import Path
from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy
from validators.kernel.patch import apply_unified_diff, PatchError


def _setup_gateway(tmp_path: Path, agent: str = "codex", allow_patterns=None, deny_patterns=None, write_mode="read-write"):
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


def test_unified_diff_engine():
    original = "line 1\nline 2\nline 3\n"
    patch = (
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " line 1\n"
        "-line 2\n"
        "+line two\n"
        "+line two and half\n"
        " line 3\n"
    )
    result = apply_unified_diff(original, patch)
    assert result == "line 1\nline two\nline two and half\nline 3\n"

    # Test context error
    bad_patch = (
        "@@ -1,3 +1,3 @@\n"
        " wrong context\n"
        "-line 2\n"
        "+line two\n"
        " line 3\n"
    )
    with pytest.raises(PatchError):
        apply_unified_diff(original, bad_patch)


def test_read_many_files(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("alpha", encoding="utf-8")
    f2.write_text("beta", encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "codex")
    res = gateway.read_many_files(["a.txt", "b.txt", "nonexistent.txt"])
    assert res["a.txt"] == "alpha"
    assert res["b.txt"] == "beta"
    assert "nonexistent.txt" not in res


def test_list_directory_and_glob(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "mod1.py").write_text("# mod1", encoding="utf-8")
    (sub / "mod2.py").write_text("# mod2", encoding="utf-8")
    (tmp_path / "root.py").write_text("# root", encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "codex")

    entries = gateway.list_directory("pkg")
    names = [e["name"] for e in entries]
    assert "mod1.py" in names
    assert "mod2.py" in names

    matches = gateway.glob("*.py")
    assert matches == ["root.py"]

    rec_matches = gateway.glob("**/*.py")
    assert "pkg/mod1.py" in rec_matches
    assert "pkg/mod2.py" in rec_matches
    assert "root.py" in rec_matches


def test_grep_and_symbols_and_references(tmp_path):
    code = (
        "class OrderManager:\n"
        "    def process_order(self, order_id):\n"
        "        pass\n"
        "\n"
        "def helper():\n"
        "    manager = OrderManager()\n"
        "    manager.process_order(123)\n"
    )
    (tmp_path / "orders.py").write_text(code, encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "codex")

    # Grep
    grep_hits = gateway.grep("process_order")
    assert len(grep_hits) == 2
    assert grep_hits[0]["file"] == "orders.py"

    # Find Symbol
    symbols = gateway.find_symbol("OrderManager")
    assert len(symbols) == 1
    assert symbols[0]["name"] == "OrderManager"
    assert symbols[0]["type"] == "class"

    fn_syms = gateway.find_symbol("process_order")
    assert len(fn_syms) == 1
    assert fn_syms[0]["name"] == "process_order"
    assert fn_syms[0]["type"] == "function"

    # Find References
    refs = gateway.find_references("OrderManager")
    assert len(refs) == 2  # class definition + instantiation


def test_apply_patch_editing(tmp_path):
    src_file = tmp_path / "main.py"
    src_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    gateway, journal = _setup_gateway(tmp_path, "codex")

    patch = (
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 42\n"
        "+    return 100\n"
    )
    gateway.apply_patch("main.py", patch)
    assert src_file.read_text(encoding="utf-8") == "def hello():\n    return 100\n"

    # Verify journal entry
    assert any(r.request.operation_type == OperationType.APPLY_PATCH for r in journal.records)


def test_move_delete_format_file(tmp_path):
    f = tmp_path / "messy.txt"
    f.write_text("line 1   \nline 2 \t \n", encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "codex")

    # Format
    gateway.format_file("messy.txt")
    assert f.read_text(encoding="utf-8") == "line 1\nline 2\n"

    # Move
    gateway.move_file("messy.txt", "clean.txt")
    assert not f.exists()
    assert (tmp_path / "clean.txt").exists()

    # Delete
    gateway.delete_file("clean.txt")
    assert not (tmp_path / "clean.txt").exists()


def test_role_enforcement_prevents_unauthorized_patch_or_mutations(tmp_path):
    src_file = tmp_path / "code.py"
    src_file.write_text("def run(): pass\n", encoding="utf-8")

    # 1. Gemma (QA) cannot apply_patch, write_file, move_file, or delete_file
    gemma_gw, _ = _setup_gateway(tmp_path, "gemma")
    with pytest.raises(PermissionError, match="policy denies operation"):
        gemma_gw.apply_patch("code.py", "@@ -1,1 +1,1 @@\n-def run(): pass\n+def run(): return 1\n")

    with pytest.raises(PermissionError, match="policy denies operation"):
        gemma_gw.delete_file("code.py")

    # 2. Claude (Architecture) cannot mutate implementation files
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.apply_patch("code.py", "@@ -1,1 +1,1 @@\n-def run(): pass\n+def run(): return 1\n")


def test_contract_denial_boundary(tmp_path):
    (tmp_path / "secret.env").write_text("KEY=12345", encoding="utf-8")
    gateway, _ = _setup_gateway(tmp_path, "codex", allow_patterns=["src/**"], deny_patterns=["*.env"])

    with pytest.raises(PermissionError, match="target is explicitly denied|outside allowed scope"):
        gateway.read_many_files(["secret.env"])
