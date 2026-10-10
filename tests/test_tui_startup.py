from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

import cli.main as main_module
import cli.tui_startup as startup
from cli.init import init
from cli.main import cli


def _disable_graph_build(monkeypatch):
    monkeypatch.setattr(startup, "graph_needs_build", lambda _workspace: True)
    monkeypatch.setattr(startup, "build_knowledge_graph", lambda _workspace: None)


def test_empty_project_is_initialized_validated_and_graph_prepared(tmp_path, monkeypatch):
    _disable_graph_build(monkeypatch)
    startup.prepare_project(tmp_path, yes=False, interactive=False)
    assert (tmp_path / ".sync" / "runtime" / "TREE.yaml").is_file()
    assert (tmp_path / "AGENTS.md").is_file()


def test_adoption_preserves_existing_project_files_and_is_idempotent(tmp_path, monkeypatch):
    originals = {
        "AGENT.md": "# Singular agent instructions\nKeep this file.\n",
        "AGENTS.md": "# Existing agent instructions\nKeep both.\n",
        "README.md": "# User README\nOriginal content.\n",
        "CHANGELOG.md": "# Changelog\nHistorical entry.\n",
        "PLAN.md": "# Project plan\nPrivate roadmap.\n",
        "VERSION": "release-channel: canary\n",
        ".gitignore": "build/\n*.local\n",
    }
    for name, contents in originals.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")

    _disable_graph_build(monkeypatch)
    startup.prepare_project(tmp_path, yes=True, interactive=False)
    readme_after_first = (tmp_path / "README.md").read_text(encoding="utf-8")
    startup.prepare_project(tmp_path, yes=True, interactive=False)

    assert (tmp_path / "AGENT.md").read_text(encoding="utf-8") == originals["AGENT.md"]
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == originals["AGENTS.md"]
    for name in ("CHANGELOG.md", "PLAN.md", "VERSION"):
        assert (tmp_path / name).read_text(encoding="utf-8") == originals[name]
    assert readme_after_first.count("<!-- stackmind-runtime:start -->") == 1
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == readme_after_first
    ignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "build/" in ignore and "*.local" in ignore
    assert ignore.count(".sync/") == 1
    assert ignore.count("graphify-out/") == 1


def test_noninteractive_adoption_requires_explicit_confirmation(tmp_path):
    (tmp_path / "README.md").write_text("# Existing", encoding="utf-8")
    with pytest.raises(click.ClickException, match="--yes"):
        startup.prepare_project(tmp_path, yes=False, interactive=False)
    assert not (tmp_path / ".sync").exists()


def test_orphaned_stackmind_marker_is_not_treated_as_empty(tmp_path, monkeypatch):
    (tmp_path / ".sync-ref").write_text("abc123\n", encoding="utf-8")
    monkeypatch.setattr(
        startup, "build_knowledge_graph", lambda _workspace: pytest.fail("must not build")
    )
    with pytest.raises(click.ClickException, match="not treated as empty"):
        startup.prepare_project(tmp_path, yes=True, interactive=False)
    assert not (tmp_path / ".sync").exists()


def test_existing_runtime_validation_failure_gates_graph_and_launch(tmp_path, monkeypatch):
    init(tmp_path, no_git=True)
    tree = tmp_path / ".sync" / "runtime" / "TREE.yaml"
    tree.unlink()
    monkeypatch.setattr(
        startup, "build_knowledge_graph", lambda _workspace: pytest.fail("must not build")
    )
    with pytest.raises(click.ClickException, match="validation failed"):
        startup.prepare_project(tmp_path, yes=False, interactive=False)


def test_existing_runtime_repairs_only_safe_missing_generated_markers(tmp_path, monkeypatch):
    init(tmp_path, no_git=True)
    marker = tmp_path / ".sync" / "runtime" / "drafts" / ".gitkeep"
    marker.unlink()
    _disable_graph_build(monkeypatch)
    startup.prepare_project(tmp_path, yes=False, interactive=False)
    assert marker.is_file()


def test_graph_is_current_when_persisted_ir_matches_source(tmp_path, monkeypatch):
    init(tmp_path, no_git=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example.py").write_text(
        "def hello():\n    return 'hi'\n", encoding="utf-8"
    )
    assert startup.graph_needs_build(tmp_path)
    startup.build_knowledge_graph(tmp_path)
    assert not startup.graph_needs_build(tmp_path)
    (tmp_path / "src" / "example.py").write_text(
        "def hello():\n    return 'changed'\n", encoding="utf-8"
    )
    assert startup.graph_needs_build(tmp_path)


def test_invalid_derived_graph_node_is_rebuilt_from_source(tmp_path, monkeypatch):
    from validators.knowledge.storage import knowledge_root

    init(tmp_path, no_git=True)
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    startup.build_knowledge_graph(tmp_path)
    node = next((knowledge_root(tmp_path) / "nodes").rglob("*.json"))
    node.write_text("{broken", encoding="utf-8")
    startup.prepare_project(tmp_path, yes=False, interactive=False)
    assert json.loads(node.read_text(encoding="utf-8"))


def test_cli_orders_readiness_before_existing_tui_launch(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        main_module, "prepare_project", lambda *args, **kwargs: order.append("project")
    )
    monkeypatch.setattr(
        main_module, "check_provider_configuration", lambda *_: order.append("provider")
    )
    monkeypatch.setattr(
        main_module,
        "ensure_daemon",
        lambda *_args, **_kwargs: (order.append("daemon") or None, "http://localhost:9999"),
    )
    monkeypatch.setattr(
        main_module,
        "tui_app",
        click.Command("fake-tui", callback=lambda **kwargs: order.append("tui")),
    )
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert order == ["project", "daemon", "provider", "tui"]
    assert "Launching StackMind TUI" in result.output


def test_cli_runs_real_project_preparation_before_launch(tmp_path, monkeypatch):
    init(tmp_path, no_git=True)
    (tmp_path / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    startup.build_knowledge_graph(tmp_path)
    order = []
    monkeypatch.setattr(
        main_module, "ensure_daemon", lambda *_args, **_kwargs: (None, "http://local")
    )
    monkeypatch.setattr(
        main_module, "check_provider_configuration", lambda *_: order.append("provider")
    )
    monkeypatch.setattr(
        main_module, "tui_app", click.Command("fake-tui", callback=lambda **_: order.append("tui"))
    )
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Validating StackMind project" in result.output
    assert "Knowledge graph is current" in result.output
    assert order == ["provider", "tui"]


def test_graph_build_failure_prevents_daemon_and_tui_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        startup,
        "build_knowledge_graph",
        lambda _workspace: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    monkeypatch.setattr(
        main_module, "ensure_daemon", lambda *_args, **_kwargs: pytest.fail("must not start daemon")
    )
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(tmp_path)])
    assert result.exit_code != 0
    assert "graph preparation failed" in result.output.lower()
    assert "TUI was not launched" in result.output


def test_provider_failure_cleans_owned_daemon_and_gates_tui(tmp_path, monkeypatch):
    stopped = []
    daemon = type("OwnedDaemon", (), {"stop": lambda self: stopped.append(True)})()
    monkeypatch.setattr(main_module, "prepare_project", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main_module, "ensure_daemon", lambda *_args, **_kwargs: (daemon, "http://local")
    )
    monkeypatch.setattr(
        main_module,
        "check_provider_configuration",
        lambda *_: (_ for _ in ()).throw(click.ClickException("provider missing")),
    )
    monkeypatch.setattr(
        main_module,
        "tui_app",
        click.Command("fake-tui", callback=lambda **_: pytest.fail("must not launch")),
    )
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(tmp_path)])
    assert result.exit_code != 0
    assert "provider missing" in result.output
    assert stopped == [True]


def test_provider_configuration_error_is_distinct_from_availability(tmp_path):
    from validators.kernel.settings import get_default_settings

    settings = get_default_settings()
    settings.roles["architecture"] = {"provider": "openai", "model": "gpt-4o"}
    path = tmp_path / ".sync" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings.to_dict()), encoding="utf-8")
    with pytest.raises(click.ClickException, match="separate from provider or model availability"):
        startup.check_provider_configuration(tmp_path)


def test_provider_defaults_are_checked_without_creating_configuration(tmp_path):
    startup.check_provider_configuration(tmp_path)
    assert not (tmp_path / ".sync" / "settings.json").exists()
    assert not (tmp_path / "settings.json").exists()


def test_daemon_reuses_healthy_endpoint_without_starting(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(startup, "_wait_for_health", lambda url, timeout: calls.append(url) or True)
    daemon, url = startup.ensure_daemon(tmp_path, "http://daemon.test")
    assert daemon is None
    assert url == "http://daemon.test"
    assert calls == ["http://daemon.test"]


def test_daemon_starts_on_ephemeral_port_after_port_conflict(tmp_path, monkeypatch):
    from validators.kernel import daemon as daemon_module

    started = []

    class FakeDaemon:
        def __init__(self, _state, port):
            self.port = port
            self.address = ("127.0.0.1", 43210)
            self.url = "http://127.0.0.1:43210"

        def start(self):
            started.append(self.port)
            if self.port != 0:
                raise OSError("port conflict")
            return self

        def stop(self):
            pass

    monkeypatch.setattr(daemon_module, "LocalDaemon", FakeDaemon)
    monkeypatch.setattr(startup, "_wait_for_health", lambda _url, _timeout: True)
    monkeypatch.setattr("cli.daemon.daemon_health", lambda _url: None)
    daemon, url = startup.ensure_daemon(tmp_path, None)
    assert daemon is not None
    assert url == "http://127.0.0.1:43210"
    assert started == [8765, 0]


def test_daemon_readiness_timeout_stops_owned_process_and_clears_pid(tmp_path, monkeypatch):
    from cli.daemon import read_daemon_pid
    from validators.kernel import daemon as daemon_module

    class FakeDaemon:
        address = ("127.0.0.1", 43211)
        url = "http://127.0.0.1:43211"
        stopped = False

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            self.stopped = True

    fake = FakeDaemon()
    monkeypatch.setattr(daemon_module, "LocalDaemon", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr("cli.daemon.daemon_health", lambda _url: None)
    monkeypatch.setattr(startup, "_wait_for_health", lambda _url, _timeout: False)
    with pytest.raises(click.ClickException, match="did not report healthy"):
        startup.ensure_daemon(tmp_path, None, timeout=0.01)
    assert fake.stopped
    assert read_daemon_pid(tmp_path)[0] is None


def test_daemon_startup_failure_is_actionable(tmp_path, monkeypatch):
    from validators.kernel import daemon as daemon_module

    class BrokenDaemon:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("listener failed")

    monkeypatch.setattr(daemon_module, "LocalDaemon", BrokenDaemon)
    monkeypatch.setattr("cli.daemon.daemon_health", lambda _url: None)
    with pytest.raises(click.ClickException, match="Could not start StackMind daemon"):
        startup.ensure_daemon(tmp_path, None)


def test_unhealthy_explicit_daemon_is_not_replaced(tmp_path, monkeypatch):
    from validators.kernel import daemon as daemon_module

    monkeypatch.setattr("cli.daemon.daemon_health", lambda _url: None)
    monkeypatch.setattr(startup, "_wait_for_health", lambda _url, _timeout: False)
    monkeypatch.setattr(
        daemon_module,
        "LocalDaemon",
        lambda *_args, **_kwargs: pytest.fail("explicit daemon URL must not start another daemon"),
    )
    with pytest.raises(click.ClickException, match="did not become healthy"):
        startup.ensure_daemon(tmp_path, "http://external", timeout=0.01)


def test_keyboard_interrupt_still_cleans_up_owned_daemon(tmp_path, monkeypatch):
    stopped = []
    daemon = type("OwnedDaemon", (), {"stop": lambda self: stopped.append(True)})()
    monkeypatch.setattr(main_module, "prepare_project", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "check_provider_configuration", lambda *_: None)
    monkeypatch.setattr(
        main_module, "ensure_daemon", lambda *_args, **_kwargs: (daemon, "http://local")
    )
    monkeypatch.setattr(
        main_module,
        "tui_app",
        click.Command("fake-tui", callback=lambda **_: (_ for _ in ()).throw(KeyboardInterrupt())),
    )
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(tmp_path)])
    assert result.exit_code != 0
    assert stopped == [True]


def test_keyboard_interrupt_during_health_wait_stops_daemon_and_clears_pid(tmp_path, monkeypatch):
    from cli.daemon import read_daemon_pid
    from validators.kernel import daemon as daemon_module

    class FakeDaemon:
        address = ("127.0.0.1", 43212)
        url = "http://127.0.0.1:43212"
        stopped = False

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            self.stopped = True

    fake = FakeDaemon()
    monkeypatch.setattr(daemon_module, "LocalDaemon", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr("cli.daemon.daemon_health", lambda _url: None)
    monkeypatch.setattr(
        startup,
        "_wait_for_health",
        lambda _url, _timeout: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        startup.ensure_daemon(tmp_path, None, timeout=0.01)
    assert fake.stopped
    assert read_daemon_pid(tmp_path)[0] is None


def test_workspace_argument_rejects_a_file(tmp_path):
    target = tmp_path / "notadir.txt"
    target.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(cli, ["tui", "--workspace", str(target)])
    assert result.exit_code == 2
    assert "Directory" in result.output
