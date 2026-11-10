"""Comprehensive multi-agent tool execution matrix test for StackMind harness."""

from pathlib import Path
import pytest

from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import ContractNormalizer
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationJournal
from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.providers.models import (
    Message,
    ProviderResponse,
    TokenUsage,
    ToolCallRequest,
)
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


def _setup_agent(tmp_path: Path, agent_name: str, wo_id: str = "WO-001") -> tuple[ToolGateway, ProviderGateway, ScratchWorkspace]:
    authoritative = tmp_path / "auth"
    ws_dir = tmp_path / f"scratch_{agent_name}"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "playbook").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "work-orders" / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)
    (authoritative / ".sync" / "reviews" / "qa").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".sync" / "playbook").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".sync" / "reviews" / "qa").mkdir(parents=True, exist_ok=True)

    contract = ContractNormalizer.normalize({
        "agent_id": agent_name,
        "work_order": wo_id,
        "scope": {"allow": ["*"], "deny": [], "write": "read-write"},
        "budget": {"max_tokens": 50000, "max_tool_calls": 50, "max_turns": 25},
    })
    boundary = RuntimeBoundary(OperationJournal())
    policy = get_role_policy(agent_name)
    workspace = ScratchWorkspace(authoritative_root=authoritative, root=ws_dir, attempt_id=f"att-{agent_name}")

    def mock_graph_query(query: str):
        return {"query": query, "nodes": 10, "edges": 15, "symbols": ["test_sym"]}

    tool_gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id=f"session-{agent_name}",
        attempt_id=f"att-{agent_name}",
        actor_id=agent_name,
        provider_id="mock",
        graph_query=mock_graph_query,
    )
    attempt = type("MockAttempt", (), {"contract": contract, "attempt_id": f"att-{agent_name}"})()
    adapter = type("MockAdapter", (), {
        "complete": lambda *a, **k: None,
        "stream": lambda *a, **k: iter([]),
    })()
    provider_gateway = ProviderGateway(adapter, tool_gateway, attempt=attempt, contract=contract)
    return tool_gateway, provider_gateway, workspace


def _dispatch(provider_gw: ProviderGateway, tool_name: str, args: dict) -> str:
    req = ToolCallRequest(id=f"call-{tool_name}", name=tool_name, arguments=args)
    return provider_gw.execute_tool_call(req)


# ─── 1. Claude (Lead Architect) Tool Matrix ───────────────────────────────


def test_exercise_claude_tools(tmp_path: Path):
    tool_gw, provider_gw, ws = _setup_agent(tmp_path, "claude")

    # 1. enter_plan_mode
    out = _dispatch(provider_gw, "enter_plan_mode", {})
    assert "plan_mode_active" in out.lower()

    # 2. write_file (planning doc)
    out = _dispatch(provider_gw, "write_file", {"path": ".sync/playbook/PLAN.md", "content": "# Architecture Roadmap\nLine 1\nLine 2\n"})
    assert "successfully wrote" in out.lower()

    # 3. read_file with line-range slicing
    out = _dispatch(provider_gw, "read_file", {"path": ".sync/playbook/PLAN.md", "start_line": 2, "end_line": 3})
    assert "Line 1" in out

    # 4. get_contract
    out = _dispatch(provider_gw, "get_contract", {})
    assert "claude" in out

    # 5. query_graph
    out = _dispatch(provider_gw, "query_graph", {"query": "auth"})
    assert "nodes" in out

    # 6. todo management
    out = _dispatch(provider_gw, "todo", {"action": "add", "task_text": "Decompose work orders"})
    assert "Decompose work orders" in out

    # 7. ask_user
    out = _dispatch(provider_gw, "ask_user", {"prompt": "Confirm scope"})
    assert "Confirm scope" in out or "ask_user" in out.lower()

    # 8. exit_plan_mode
    out = _dispatch(provider_gw, "exit_plan_mode", {})
    assert "plan_mode_inactive" in out.lower()


# ─── 2. Codex (Backend Implementation) Tool Matrix ─────────────────────────


def test_exercise_codex_tools(tmp_path: Path):
    tool_gw, provider_gw, ws = _setup_agent(tmp_path, "codex")

    # 1. write_file
    out = _dispatch(provider_gw, "write_file", {"path": "src/api.py", "content": "def run():\n    return 'old_val'\n"})
    assert "successfully wrote" in out.lower()

    # 2. edit_file (new tool)
    out = _dispatch(provider_gw, "edit_file", {"target": "src/api.py", "search_text": "return 'old_val'", "replace_text": "return 'new_val'"})
    assert "successfully edited" in out.lower()
    assert (ws.root / "src/api.py").read_text(encoding="utf-8") == "def run():\n    return 'new_val'\n"

    # 3. read_file with line slicing
    out = _dispatch(provider_gw, "read_file", {"path": "src/api.py", "start_line": 1, "end_line": 1})
    assert "def run():" in out

    # 4. batch_write_files
    out = _dispatch(provider_gw, "batch_write_files", {
        "files": [
            {"path": "src/models.py", "content": "class User:\n    pass\n"},
            {"path": "src/utils.py", "content": "def helper():\n    return True\n"},
        ]
    })
    assert "successfully wrote 2 file(s)" in out.lower()

    # 5. run_lint
    out = _dispatch(provider_gw, "run_lint", {"path": "src/api.py"})
    assert "success" in out.lower()

    # 6. run_typecheck
    out = _dispatch(provider_gw, "run_typecheck", {"path": "src/api.py"})
    assert "success" in out.lower()

    # 7. find_symbol
    out = _dispatch(provider_gw, "find_symbol", {"name": "User"})
    assert "User" in out

    # 8. find_references
    out = _dispatch(provider_gw, "find_references", {"symbol": "User"})
    assert "models.py" in out

    # 9. list_directory & glob & grep
    out_dir = _dispatch(provider_gw, "list_directory", {"path": "src"})
    assert "api.py" in out_dir
    out_glob = _dispatch(provider_gw, "glob", {"base_dir": "src", "pattern": "*.py"})
    assert "api.py" in out_glob
    out_grep = _dispatch(provider_gw, "grep", {"pattern": "def run"})
    assert "api.py" in out_grep

    # 10. submit_for_review
    out_sub = _dispatch(provider_gw, "submit_for_review", {})
    assert "submitted" in out_sub.lower()


# ─── 3. Gemini (Frontend Implementation & UI) Tool Matrix ──────────────────


def test_exercise_gemini_tools(tmp_path: Path):
    tool_gw, provider_gw, ws = _setup_agent(tmp_path, "gemini")

    # 1. write_file HTML
    out = _dispatch(provider_gw, "write_file", {"path": "frontend/index.html", "content": "<!DOCTYPE html>\n<html><body><h1>Old Header</h1></body></html>\n"})
    assert "successfully wrote" in out.lower()

    # 2. edit_file (new tool on frontend)
    out = _dispatch(provider_gw, "edit_file", {"target": "frontend/index.html", "search_text": "<h1>Old Header</h1>", "replace_text": "<h1>New Header</h1>"})
    assert "successfully edited" in out.lower()
    assert "<h1>New Header</h1>" in (ws.root / "frontend/index.html").read_text(encoding="utf-8")

    # 3. read_file with line slicing
    out = _dispatch(provider_gw, "read_file", {"path": "frontend/index.html", "start_line": 2, "end_line": 2})
    assert "<h1>New Header</h1>" in out

    # 4. browser suite (Gemini exclusive)
    out = _dispatch(provider_gw, "browser_open", {"url": "http://localhost:5173"})
    assert "open" in out.lower()
    out = _dispatch(provider_gw, "browser_navigate", {"url": "http://localhost:5173/dashboard"})
    assert "navigated" in out.lower()
    out = _dispatch(provider_gw, "browser_click", {"selector": "button#login"})
    assert "clicked" in out.lower()
    out = _dispatch(provider_gw, "browser_type", {"selector": "input#email", "text": "test@example.com"})
    assert "typed" in out.lower()
    out = _dispatch(provider_gw, "browser_screenshot", {"output_path": "frontend/shot.png"})
    assert "shot.png" in out.lower()
    out = _dispatch(provider_gw, "inspect_screenshot", {"screenshot_path": "frontend/shot.png"})
    assert "width" in out.lower() or "status" in out.lower() or "shot.png" in out.lower() or "exists" in out.lower()

    # 5. submit_for_review
    out_sub = _dispatch(provider_gw, "submit_for_review", {})
    assert "submitted" in out_sub.lower()


# ─── 4. Gemma (QA Reviewer & Gate Authority) Tool Matrix ───────────────────


def test_exercise_gemma_tools(tmp_path: Path):
    tool_gw, provider_gw, ws = _setup_agent(tmp_path, "gemma")

    # Prepare deliverable to inspect
    (ws.root / "src").mkdir(parents=True, exist_ok=True)
    (ws.root / "src/auth.py").write_text("def auth():\n    return True\n", encoding="utf-8")

    # 1. read_file
    out = _dispatch(provider_gw, "read_file", {"path": "src/auth.py", "start_line": 1, "end_line": 2})
    assert "def auth():" in out

    # 2. verify_deliverable
    out = _dispatch(provider_gw, "verify_deliverable", {"wo_id": "WO-001", "deliverable_path": "src/auth.py"})
    assert "exist" in out.lower() or "valid" in out.lower() or "deliverable" in out.lower()

    # 3. verify_tests
    out = _dispatch(provider_gw, "verify_tests", {"test_summary": "15 passed, 0 failed"})
    assert "verified" in out.lower() or "test" in out.lower()

    # 4. verify_diff
    out = _dispatch(provider_gw, "verify_diff", {"wo_id": "WO-001"})
    assert "diff" in out.lower() or "scope" in out.lower()

    # 5. verify_provenance
    out = _dispatch(provider_gw, "verify_provenance", {"wo_id": "WO-001"})
    assert "verified" in out.lower()

    # 6. submit_qa_verdict
    out = _dispatch(provider_gw, "submit_qa_verdict", {
        "wo_id": "WO-001",
        "verdict": "PASSED",
        "report": "All 15 tests passed cleanly with no security findings",
    })
    assert "qa verdict" in out.lower() or "passed" in out.lower()

    # 7. approve_work_order
    out = _dispatch(provider_gw, "approve_work_order", {"wo_id": "WO-001"})
    assert "approved" in out.lower()

    # 8. request_changes
    out = _dispatch(provider_gw, "request_changes", {"wo_id": "WO-001", "requested_changes": ["Add docstring"]})
    assert "changes" in out.lower()


# ─── 5. Local-LLM (GitOps & Release Management) Tool Matrix ────────────────


def test_exercise_local_llm_tools(tmp_path: Path):
    tool_gw, provider_gw, ws = _setup_agent(tmp_path, "local-llm")

    # 1. write_file & edit_file version declarations
    out = _dispatch(provider_gw, "write_file", {"path": "VERSION", "content": "1.0.0\n"})
    assert "successfully wrote" in out.lower()
    out = _dispatch(provider_gw, "edit_file", {"target": "VERSION", "search_text": "1.0.0", "replace_text": "1.0.1"})
    assert "successfully edited" in out.lower()
    assert (ws.root / "VERSION").read_text(encoding="utf-8") == "1.0.1\n"

    # 2. read_file with line slicing
    out = _dispatch(provider_gw, "read_file", {"path": "VERSION", "start_line": 1, "end_line": 1})
    assert "1.0.1" in out

    # 3. git inspection tools
    out = _dispatch(provider_gw, "git_status", {})
    assert "clean" in out.lower() or "staged" in out.lower()
    out = _dispatch(provider_gw, "git_branch", {})
    assert "branch" in out.lower() or "head" in out.lower() or "master" in out.lower() or "main" in out.lower()

    # 4. update_changelog
    out = _dispatch(provider_gw, "update_changelog", {"entry": "### 1.0.1\n- Added edit_file tool\n"})
    assert "updated" in out.lower()

    # 5. update_version
    out = _dispatch(provider_gw, "update_version", {"version": "1.0.1"})
    assert "version" in out.lower()

    # 6. validate_release_metadata
    out = _dispatch(provider_gw, "validate_release_metadata", {})
    assert "release" in out.lower() or "valid" in out.lower() or "metadata" in out.lower()
