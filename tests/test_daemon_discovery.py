"""Daemon ownership, discovery, and command lifecycle tests for WO-024."""

from __future__ import annotations

import os
import socket

from click.testing import CliRunner

import cli.tui.app as tui_app
import cli.daemon as daemon_cli
from cli.daemon import clear_daemon_pid, daemon_group, daemon_health, read_daemon_pid, write_daemon_pid
from validators.kernel.daemon import LocalDaemon


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_tui_attaches_to_healthy_default_daemon(tmp_path, monkeypatch):
    with LocalDaemon(tmp_path, port=0) as daemon:
        monkeypatch.setattr(tui_app, "DEFAULT_DAEMON_PORT", daemon.address[1])
        attached, url, is_attached = tui_app.resolve_tui_daemon(tmp_path)
        assert attached is None
        assert url == daemon.url
        assert is_attached is True


def test_tui_starts_default_daemon_and_records_ownership(tmp_path, monkeypatch):
    port = _available_port()
    monkeypatch.setattr(tui_app, "DEFAULT_DAEMON_PORT", port)
    daemon, url, is_attached = tui_app.resolve_tui_daemon(tmp_path)
    try:
        pid, port = read_daemon_pid(tmp_path)
        assert daemon is not None
        assert is_attached is False
        assert port == daemon.address[1]
        assert pid == os.getpid()
        assert daemon_health(url) == {"status": "ok", "sessions": 0}
    finally:
        if daemon is not None:
            daemon.stop()
        clear_daemon_pid(tmp_path)


def test_daemon_status_reports_health_pid_and_sessions(tmp_path):
    with LocalDaemon(tmp_path, port=0) as daemon:
        write_daemon_pid(tmp_path, daemon.address[1], pid=12345)
        result = CliRunner().invoke(daemon_group, ["status", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "State: online" in result.output
    assert "PID: 12345" in result.output
    assert "Active sessions: 0" in result.output


def test_daemon_stop_terminates_tracked_pid_and_clears_state(tmp_path, monkeypatch):
    write_daemon_pid(tmp_path, 8765, pid=12345)
    terminated: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_cli.os, "kill", lambda pid, sig: terminated.append((pid, sig)))
    monkeypatch.setattr(daemon_cli, "_wait_until_offline", lambda url: True)

    result = CliRunner().invoke(daemon_group, ["stop", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert terminated == [(12345, daemon_cli.signal.SIGTERM)]
    assert read_daemon_pid(tmp_path)[0] is None


def test_tui_detachment_does_not_stop_externally_owned_daemon(tmp_path, monkeypatch):
    with LocalDaemon(tmp_path, port=0) as daemon:
        monkeypatch.setattr(tui_app, "DEFAULT_DAEMON_PORT", daemon.address[1])
        owned, url, is_attached = tui_app.resolve_tui_daemon(tmp_path)
        assert owned is None and is_attached
        assert daemon_health(url) is not None
