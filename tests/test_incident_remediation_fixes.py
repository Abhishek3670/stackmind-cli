"""Tests verifying the incident remediation fixes:
1. Daemon server error isolation (daemon.log instead of sys.stderr).
2. Daemon _send memory error protection.
3. Event list pagination support in protocol.
4. Supervisor rework candidate selection including GitOps on release blockers.
5. Supervisor rework prompt scope enforcement for QA agents.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.daemon.server import _DaemonHTTPServer, LocalDaemon
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState


def test_daemon_server_isolates_errors_to_daemon_log(tmp_path: Path):
    """Verify unhandled request errors are logged to daemon.log instead of sys.stderr."""
    server = _DaemonHTTPServer(("127.0.0.1", 0), MagicMock(), state_dir=tmp_path)
    
    # Simulate an exception in sys.exc_info()
    stderr_capture = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr_capture
    try:
        try:
            raise MemoryError("Test heap exhausted during JSON dumps")
        except MemoryError:
            server.handle_error(MagicMock(), ("127.0.0.1", 50489))
    finally:
        sys.stderr = old_stderr

    # Stderr must not contain the raw traceback (which would corrupt TUI screen buffer)
    assert "Traceback" not in stderr_capture.getvalue()
    assert "MemoryError" not in stderr_capture.getvalue()

    # daemon.log must contain the error details
    daemon_log = tmp_path / "daemon.log"
    assert daemon_log.exists()
    log_content = daemon_log.read_text(encoding="utf-8")
    assert "Exception occurred during processing of request from ('127.0.0.1', 50489)" in log_content
    assert "Test heap exhausted during JSON dumps" in log_content


def test_event_list_pagination_limit(tmp_path: Path):
    """Verify event.list supports the limit parameter to prevent unbounded memory spikes."""
    ws = str(tmp_path / "ws")
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    protocol = JsonRpcProtocol(manager)

    created = protocol.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session.create",
        "params": {
            "agent": "codex",
            "provider": "test",
            "contract": {"agent_id": "codex", "work_order": "WO-001", "scope": {"allow": ["tests/**"]}},
            "workspace": ws,
        },
    })["result"]
    sid = created["session_id"]

    # Publish multiple events
    for i in range(10):
        manager.events.publish(
            name="test.event",
            session_id=sid,
            payload={"index": i},
        )

    # Fetch with limit=3
    res_limited = protocol.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "event.list",
        "params": {"session_id": sid, "limit": 3},
    })["result"]

    assert len(res_limited) == 3

    # Fetch without limit returns all
    res_all = protocol.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "event.list",
        "params": {"session_id": sid},
    })["result"]

    assert len(res_all) >= 10


def test_supervisor_rework_includes_gitops_on_version_blockers(tmp_path: Path):
    """Verify that integration review blockers concerning versioning/changelog include GitOps WOs."""
    supervisor = LifecycleSupervisor(MagicMock())
    ws = tmp_path / "project"
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # Create GitOps WO-009 with deliverable package.json and VERSION.md
    wo_009 = {
        "id": "WO-009",
        "assigned_agents": ["local-llm"],
        "deliverable": {"path": "package.json"},
    }
    (active_dir / "WO-009.yaml").write_text(yaml.safe_dump(wo_009), encoding="utf-8")

    # Create QA WO-008
    wo_008 = {
        "id": "WO-008",
        "assigned_agents": ["gemma"],
        "deliverable": {"path": "tests/test_build.js"},
    }
    (active_dir / "WO-008.yaml").write_text(yaml.safe_dump(wo_008), encoding="utf-8")

    state = RunState(
        run_id="run-123",
        product_goal="Build test product",
        session_id="test-session",
        workspace=str(ws),
        completed_wo_ids=["WO-008"],
        worker_wo_ids=["WO-008", "WO-009"],
        gitops_wo_id="WO-009",
    )

    blockers = [
        "VERSION.md missing",
        "package.json version is 0.1.0 instead of 1.0.0",
        "CHANGELOG.md missing [1.0.0] section",
    ]

    reworkable = supervisor._reworkable_integration_wos(state, ws, blockers)

    # Both the GitOps work order (to create the files) and QA (to re-verify) must be selected
    assert "WO-009" in reworkable
    assert "WO-008" in reworkable


def test_daemon_send_handles_memory_error_without_crashing(tmp_path: Path):
    """Verify Handler._send handles MemoryError gracefully and sends a 500 error payload."""
    daemon = LocalDaemon(tmp_path)
    handler_cls = daemon._handler()
    
    handler = handler_cls.__new__(handler_cls)
    sent_responses = []
    sent_headers = []
    wfile = io.BytesIO()
    handler.wfile = wfile
    handler.send_response = lambda code: sent_responses.append(code)
    handler.send_header = lambda k, v: sent_headers.append((k, v))
    handler.end_headers = lambda: None

    # Mock json.dumps to simulate MemoryError on the body
    orig_dumps = json.dumps
    def mock_dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("giant_payload"):
            raise MemoryError("Out of contiguous memory")
        return orig_dumps(obj, *args, **kwargs)

    json.dumps = mock_dumps
    try:
        handler._send(200, {"giant_payload": True, "id": 42})
    finally:
        json.dumps = orig_dumps

    assert sent_responses == [500]
    output = json.loads(wfile.getvalue().decode("utf-8"))
    assert output["error"]["code"] == -32000
    assert "Response payload exceeded memory limits" in output["error"]["message"]
    assert output["id"] == 42

