from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from validators.kernel import (
    AuthorizationPolicy,
    ContractNormalizer,
    GovernedToolRegistry,
    McpServer,
    OperatingMode,
    OperatingModeTracker,
    OperationJournal,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
)
from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager


def runtime(tmp_path: Path):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir()
    (authoritative / "source.txt").write_text("source", encoding="utf-8")
    workspace = ScratchWorkspace.create(authoritative, "mcp-attempt")
    contract = ContractNormalizer.normalize(
        {
            "agent": "codex",
            "wo": "WO-006",
            "scope": {
                "allow": ["workspace/**", "graph/**"],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 12},
        }
    )
    manager = SessionManager(DaemonStorage(tmp_path / "daemon"))
    session = manager.create_session("codex", "test", {"work_order": "WO-006"}, str(workspace.root))
    gateway = ToolGateway(
        workspace,
        RuntimeBoundary(OperationJournal()),
        contract,
        AuthorizationPolicy.permit(
            "test", ["read_file", "write_file", "run_command", "query_graph"]
        ),
        session["session_id"],
        "mcp-attempt",
        "codex",
        "test",
        lambda query: {"query": query},
    )
    modes = OperatingModeTracker()
    registry = GovernedToolRegistry(
        gateway,
        manager,
        session["session_id"],
        contract,
        modes,
        context_provider=lambda query: {"context": query},
        review_submitter=lambda root: {"review_root": str(root)},
    )
    return workspace, manager, session, registry, modes


def rpc(server: McpServer, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return server.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


def call(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    response = rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})
    return json.loads(response["result"]["content"][0]["text"])


def test_mcp_handshake_schema_and_stdio(tmp_path):
    *_, registry, _ = runtime(tmp_path)
    server = McpServer(registry)
    initialized = rpc(server, "initialize", {"protocolVersion": "2024-11-05"})
    assert initialized["result"]["capabilities"] == {"tools": {}}
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert {item["name"] for item in tools} == {
        "stackmind.read_file",
        "stackmind.write_file",
        "stackmind.run_command",
        "stackmind.query_graph",
        "stackmind.get_context",
        "stackmind.get_contract",
        "stackmind.submit_for_review",
    }
    output = io.StringIO()
    server.serve_stdio(io.StringIO('{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), output)
    assert json.loads(output.getvalue()) == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_all_governed_tools_are_scratch_journaled_and_reviewable(tmp_path):
    workspace, manager, session, registry, modes = runtime(tmp_path)
    server = McpServer(registry)
    assert call(server, "stackmind.read_file", {"path": "source.txt"}) == "source"
    assert call(server, "stackmind.write_file", {"path": "echo_ok.py", "content": "print('ok')"}) == {
        "written": True
    }
    command = call(
        server,
        "stackmind.run_command",
        # Interpreter denylist (Phase 2): code-string flags are denied at the
        # agent boundary, so this vector runs a script file instead of -c.
        {"command": [sys.executable, "echo_ok.py"]},
    )
    assert command["returncode"] == 0 and command["stdout"] == "ok\n"
    assert call(server, "stackmind.query_graph", {"query": "ToolGateway"}) == {
        "query": "ToolGateway"
    }
    assert call(server, "stackmind.get_context", {"query": "task"}) == {"context": "task"}
    contract = call(server, "stackmind.get_contract")
    assert contract["learning_eligible"] is True and contract["work_order"] == "WO-006"
    review = call(server, "stackmind.submit_for_review")
    assert review["verified"] is True and workspace.change_set() == workspace.root
    assert modes.state_for(session["session_id"]).mode is OperatingMode.GOVERNED
    assert len(manager.get_session(session["session_id"])["journal"]) == 7


def test_unmanaged_mode_is_explicitly_ineligible_and_scope_rejections_are_safe(tmp_path):
    *_, session, registry, modes = runtime(tmp_path)
    server = McpServer(registry)
    modes.mark_unmanaged(session["session_id"])
    contract = call(server, "stackmind.get_contract")
    assert contract["mode"] == "unmanaged" and contract["learning_eligible"] is False
    rejected = rpc(
        server,
        "tools/call",
        {"name": "stackmind.write_file", "arguments": {"path": "../live", "content": "bad"}},
    )
    assert rejected["result"]["isError"] is True
    assert "traversal" in rejected["result"]["content"][0]["text"]


def test_mcp_http_transport_uses_local_daemon(tmp_path):
    *_, registry, _ = runtime(tmp_path)
    server = McpServer(registry)
    with LocalDaemon(tmp_path / "http", mcp_protocol=server.protocol) as daemon:
        request = Request(
            f"{daemon.url}/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
    assert len(payload["result"]["tools"]) == 7
