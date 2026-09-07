"""Persistent-session exit gate tests for Phase P4."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-005", "scope": {"allow": ["tests/**"]}}


def _rpc(daemon: LocalDaemon, method: str, params: dict[str, object], request_id: int = 1) -> dict:
    request = Request(f"{daemon.url}/rpc", data=json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
    }).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        return json.loads(response.read())


def test_client_reconnects_and_streams_lifecycle_events(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        created = _rpc(daemon, "session.create", {
            "agent": "codex", "provider": "test", "contract": _contract(), "workspace": "workspace",
        })["result"]
        attached = _rpc(
            daemon, "session.attach", {"session_id": created["session_id"]}, 2
        )["result"]
        events = _rpc(daemon, "event.list", {"session_id": created["session_id"]}, 3)["result"]
        assert attached["session_id"] == created["session_id"]
        assert [event["name"] for event in events][:3] == [
            "session.started", "attempt.started", "contract.loaded",
        ]
        with urlopen(f"{daemon.url}/health") as response:
            assert json.loads(response.read()) == {"status": "ok", "sessions": 1}


def test_active_operation_cancels_mid_turn_and_is_journaled(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    cancellation = manager.begin_operation(session["session_id"], "provider.call")
    operation_id = manager._sessions[session["session_id"]]["active_operation"]
    manager.cancel_session(session["session_id"])
    assert cancellation.is_set()
    manager.complete_operation(session["session_id"], operation_id, {"reason": "cancelled"})
    restored = manager.get_session(session["session_id"])
    assert restored["state"] == "CANCELLED"
    assert restored["journal"][0]["status"] == "CANCELLED"


def test_state_and_audit_trail_recover_after_daemon_restart(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session(
        "codex", "test", _contract(), "workspace", session_id="recover-me"
    )
    manager.record_verification(session["session_id"], {"passed": True})
    manager.record_experience(session["session_id"], "EXP-1")

    recovered = SessionManager(DaemonStorage(tmp_path))
    assert recovered.get_session("recover-me")["state"] == "WAITING"
    names = [event.name for event in recovered.events.events("recover-me")]
    assert "session.recovered" in names
    assert {"verification.started", "verification.completed", "experience.recorded"} <= set(names)
