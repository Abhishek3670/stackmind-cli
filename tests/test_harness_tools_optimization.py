"""Unit tests for newly added tools and optimizations in StackMind harness."""

import pytest
from pathlib import Path
from typing import Any

from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import ContractNormalizer
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.mcp.tools import GovernedToolRegistry
from validators.kernel.operations import OperationJournal, OperationType
from validators.kernel.providers.gateway import (
    ProviderGateway,
    STANDARD_KERNEL_TOOLS,
    WORKER_CODE_CORE_TOOL_NAMES,
    truncate_tool_output,
)
from validators.kernel.providers.models import (
    Message,
    ProviderResponse,
    TokenUsage,
    ToolCallRequest,
)
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


def _setup_test_gateway(tmp_path: Path, role: str = "codex") -> tuple[ToolGateway, ScratchWorkspace]:
    authoritative = tmp_path / "authoritative"
    ws_dir = tmp_path / "scratch"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)

    contract = ContractNormalizer.normalize({
        "agent_id": role,
        "work_order": "WO-001",
        "scope": {"allow": ["*"], "deny": [], "write": "read-write"},
        "budget": {"max_tokens": 10000, "max_tool_calls": 20, "max_turns": 10},
    })
    boundary = RuntimeBoundary(OperationJournal())
    policy = AuthorizationPolicy.permit(
        f"policy-{role}",
        (
            OperationType.READ_FILE.value,
            OperationType.WRITE_FILE.value,
            OperationType.EDIT_FILE.value,
            OperationType.APPLY_PATCH.value,
            OperationType.FIND_SYMBOL.value,
            OperationType.FIND_REFERENCES.value,
            OperationType.RUN_LINT.value,
            OperationType.RUN_TYPECHECK.value,
        ),
    )
    workspace = ScratchWorkspace(authoritative_root=authoritative, root=ws_dir, attempt_id="test-attempt-1")
    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="test-session-1",
        attempt_id="test-attempt-1",
        actor_id=role,
        provider_id="mock",
    )
    return gateway, workspace


class MockSingleAdapter:
    def __init__(self, response: ProviderResponse):
        self.response = response

    def complete(self, *args, **kwargs):
        return self.response

    def stream(self, *args, **kwargs):
        yield type("Chunk", (), {"content": "", "delta": "", "tool_calls": [], "usage": None})()


# ─── 1. edit_file Tests ───────────────────────────────────────────────────


def test_edit_file_success(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "sample.py"
    test_file.write_text("def hello():\n    return 'old'\n", encoding="utf-8")

    gateway.edit_file("sample.py", "return 'old'", "return 'new'")

    assert test_file.read_text(encoding="utf-8") == "def hello():\n    return 'new'\n"
    assert "sample.py" in gateway.written_files


def test_edit_file_search_text_not_found(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "sample.py"
    test_file.write_text("def hello():\n    return 'old'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="search_text not found in 'sample.py'"):
        gateway.edit_file("sample.py", "return 'nonexistent'", "return 'new'")


def test_edit_file_ambiguous_match_raises_error(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "sample.py"
    test_file.write_text("item = 1\nitem = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="occurs 2 times in 'sample.py'"):
        gateway.edit_file("sample.py", "item = 1", "item = 2")


def test_edit_file_empty_search_raises_error(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "sample.py"
    test_file.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError, match="search_text must not be empty"):
        gateway.edit_file("sample.py", "", "new")


def test_edit_file_permission_denied_when_not_permitted(tmp_path: Path):
    authoritative = tmp_path / "authoritative"
    ws_dir = tmp_path / "scratch"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)

    contract = ContractNormalizer.normalize({
        "agent_id": "reader",
        "work_order": "WO-001",
        "scope": {"allow": ["*"], "deny": [], "write": "read-only"},
        "budget": {"max_tokens": 10000, "max_tool_calls": 20, "max_turns": 10},
    })
    boundary = RuntimeBoundary(OperationJournal())
    policy = AuthorizationPolicy.permit("policy-readonly", (OperationType.READ_FILE.value,))
    workspace = ScratchWorkspace(authoritative_root=authoritative, root=ws_dir, attempt_id="test-attempt-2")
    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="test-session-2",
        attempt_id="test-attempt-2",
        actor_id="reader",
        provider_id="mock",
    )
    test_file = ws_dir / "read.py"
    test_file.write_text("a = 1", encoding="utf-8")

    with pytest.raises(PermissionError):
        gateway.edit_file("read.py", "a = 1", "a = 2")


# ─── 2. read_file Line-Range Slicing Tests ───────────────────────────────


def test_read_file_full_content_backward_compatibility(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "lines.txt"
    content = "line 1\nline 2\nline 3\n"
    test_file.write_text(content, encoding="utf-8")

    assert gateway.read_file("lines.txt") == content


def test_read_file_line_range_slicing(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "lines.txt"
    lines = [f"line {i}\n" for i in range(1, 11)]
    test_file.write_text("".join(lines), encoding="utf-8")

    sliced = gateway.read_file("lines.txt", start_line=3, end_line=5)
    assert sliced == "line 3\nline 4\nline 5\n"


def test_read_file_with_line_numbers(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "lines.txt"
    test_file.write_text("first\nsecond\nthird\n", encoding="utf-8")

    numbered = gateway.read_file("lines.txt", start_line=2, end_line=3, line_numbers=True)
    assert "    2 | second\n" in numbered
    assert "    3 | third\n" in numbered


def test_read_file_start_beyond_eof(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    test_file = ws.root / "lines.txt"
    test_file.write_text("one\ntwo\n", encoding="utf-8")

    res = gateway.read_file("lines.txt", start_line=10, end_line=20)
    assert "[Line 10 is beyond end of file" in res


# ─── 3. truncate_tool_output Head + Tail Tests ───────────────────────────


def test_truncate_tool_output_short_content_untouched():
    short = "All 10 tests passed."
    assert truncate_tool_output(short, max_chars=100) == short


def test_truncate_tool_output_preserves_head_and_tail():
    head = "START OF EXECUTION LOG:\n"
    tail = "FAILED: assert False == True\nAssertionError in test_critical_feature"
    middle = "x" * 2000
    long_output = f"{head}{middle}\n{tail}"

    truncated = truncate_tool_output(long_output, max_chars=500)
    assert len(truncated) <= 650
    assert "START OF EXECUTION LOG" in truncated
    assert "AssertionError in test_critical_feature" in truncated
    assert "[... TRUNCATED:" in truncated


# ─── 4. run_lint & run_typecheck Hardening Tests ─────────────────────────


def test_run_lint_valid_python(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    valid_file = ws.root / "valid.py"
    valid_file.write_text("x: int = 42\ndef get_val() -> int:\n    return x\n", encoding="utf-8")

    result = gateway.run_lint("valid.py")
    assert result["success"] is True
    assert len(result["errors"]) == 0


def test_run_lint_syntax_error(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    broken_file = ws.root / "broken.py"
    broken_file.write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = gateway.run_lint("broken.py")
    assert result["success"] is False
    assert len(result["errors"]) > 0


def test_run_typecheck_valid(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    py_file = ws.root / "typed.py"
    py_file.write_text("a: int = 1\nb: str = 'text'\n", encoding="utf-8")

    result = gateway.run_typecheck("typed.py")
    assert result["success"] is True


# ─── 5. ProviderGateway Tool Dispatch Integration Tests ───────────────────


def test_provider_gateway_dispatches_edit_file(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    target = ws.root / "module.py"
    target.write_text("version = '1.0.0'\n", encoding="utf-8")

    contract = gateway.contract
    attempt = type("MockAttempt", (), {"contract": contract, "attempt_id": "att-1"})()

    response = ProviderResponse(
        message=Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="edit_file",
                    arguments={"target": "module.py", "search_text": "1.0.0", "replace_text": "1.0.1"},
                )
            ],
        ),
        usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    provider_gateway = ProviderGateway(MockSingleAdapter(response), gateway, attempt=attempt, contract=contract)
    output = provider_gateway.execute_tool_call(response.message.tool_calls[0])

    assert "Successfully edited module.py" in output
    assert target.read_text(encoding="utf-8") == "version = '1.0.1'\n"
    assert "module.py" in provider_gateway.written_files


def test_standard_kernel_tools_includes_edit_file():
    tool_names = {t.name for t in STANDARD_KERNEL_TOOLS}
    assert "edit_file" in tool_names
    assert "edit_file" in WORKER_CODE_CORE_TOOL_NAMES

    read_file_tool = next(t for t in STANDARD_KERNEL_TOOLS if t.name == "read_file")
    assert "start_line" in read_file_tool.parameters["properties"]
    assert "end_line" in read_file_tool.parameters["properties"]


# ─── 6. MCP GovernedToolRegistry Tests ────────────────────────────────────


def test_mcp_registry_supports_edit_file(tmp_path: Path):
    gateway, ws = _setup_test_gateway(tmp_path)
    target = ws.root / "mcp_target.py"
    target.write_text("data = 'old_mcp'\n", encoding="utf-8")

    manager = type("MockManager", (), {
        "begin_operation": lambda self, sid, name: ("tok", "op-mcp-1"),
        "complete_operation": lambda self, sid, op_id, res: None,
    })()

    registry = GovernedToolRegistry(gateway, manager, "session-mcp", gateway.contract)
    defs = {d["name"]: d for d in registry.definitions()}
    assert "stackmind.edit_file" in defs

    result = registry.call(
        "stackmind.edit_file",
        {"path": "mcp_target.py", "search_text": "old_mcp", "replace_text": "new_mcp"},
    )
    assert result == {"edited": True}
    assert target.read_text(encoding="utf-8") == "data = 'new_mcp'\n"
