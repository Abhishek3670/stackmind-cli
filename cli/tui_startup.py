"""Safe project preparation for the unified ``stackmind tui`` command."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import click


class StartupStep:
    """Animated startup step that spins a rotating '*' in interactive terminals
    and captures verbose internal output.
    """

    FRAMES = ["|", "/", "-", "\\"]

    def __init__(
        self,
        message: str,
        complete_message: str | None = None,
        *,
        is_tty: bool | None = None,
    ) -> None:
        self.message = message
        self.complete_message = complete_message
        self.is_tty = (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()) if is_tty is None else is_tty
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stdout_buf = io.StringIO()
        self._stderr_buf = io.StringIO()
        self._console_stream = None
        self._redirect_out = None
        self._redirect_err = None

    def set_message(self, message: str) -> None:
        """Dynamically update in-progress message."""
        self.message = message
        if not self.is_tty and self._console_stream:
            self._console_stream.write(f"[*] {self.message}\n")
            self._console_stream.flush()

    def set_complete_message(self, message: str) -> None:
        """Dynamically update completed message."""
        self.complete_message = message

    def _spin(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            frame = self.FRAMES[idx % len(self.FRAMES)]
            bullet = click.style(f"[{frame}]", fg="cyan", bold=True)
            try:
                self._console_stream.write(f"\r{bullet} {self.message.ljust(60)}")
                self._console_stream.flush()
            except Exception:
                pass
            idx += 1
            time.sleep(0.08)

    def __enter__(self):
        self._console_stream = sys.stdout
        if self.is_tty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            self._console_stream.write(f"[*] {self.message}\n")
            self._console_stream.flush()

        self._redirect_out = contextlib.redirect_stdout(self._stdout_buf)
        self._redirect_err = contextlib.redirect_stderr(self._stderr_buf)
        self._redirect_out.__enter__()
        self._redirect_err.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._redirect_err:
            self._redirect_err.__exit__(exc_type, exc_val, exc_tb)
        if self._redirect_out:
            self._redirect_out.__exit__(exc_type, exc_val, exc_tb)

        if self.is_tty:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=0.5)

        done_text = self.complete_message or f"{self.message} (done)"
        if exc_type is not None:
            bullet = click.style("[x]", fg="red", bold=True)
            if self.is_tty:
                self._console_stream.write(f"\r{bullet} {self.message} (failed)\n")
            else:
                self._console_stream.write(f"{bullet} {self.message} (failed)\n")
        else:
            bullet = click.style("[+]", fg="green", bold=True)
            if self.is_tty:
                self._console_stream.write(f"\r{bullet} {done_text.ljust(60)}\n")
            else:
                self._console_stream.write(f"{bullet} {done_text}\n")
        self._console_stream.flush()
        return False


def project_state(workspace: Path) -> str:
    """Classify a workspace without mutating it."""
    workspace = workspace.resolve()
    sync_path = workspace / ".sync"
    if sync_path.exists():
        return "stackmind"
    if (workspace / ".sync-ref").exists():
        return "damaged"
    if not workspace.exists() or not any(workspace.iterdir()):
        return "empty"
    return "adopt"


def prepare_project(workspace: Path, *, yes: bool, interactive: bool) -> None:
    """Initialize/adopt or validate an existing StackMind runtime."""
    from cli.init import init as initialize
    from cli.validate import validate

    workspace = workspace.resolve()
    state = project_state(workspace)

    if state == "damaged":
        raise click.ClickException(
            "Found StackMind project markers but no .sync runtime. It was not "
            "treated as empty. Restore the runtime or move the orphaned marker, "
            "then run `stackmind tui` again."
        )

    if state in {"empty", "adopt"}:
        if state == "adopt":
            click.echo(
                "Adoption will add the .sync runtime and missing StackMind files; "
                "existing README.md, PLAN.md, CHANGELOG.md, VERSION, and .gitignore "
                "content will be preserved."
            )
            if not yes:
                if not interactive:
                    raise click.ClickException(
                        "Adoption needs confirmation in a non-interactive shell; "
                        "rerun with `stackmind tui --yes`."
                    )
                if not click.confirm("Adopt this project into StackMind?", default=False):
                    raise click.ClickException("Project adoption cancelled.")
        with StartupStep(
            f"Initializing StackMind runtime ({workspace.name})...",
            complete_message=f"Initialized StackMind runtime ({workspace.name})",
        ):
            try:
                # Adoption keeps git untouched (never init or commit inside a
                # project we do not own); an empty directory gets the same git
                # initialization and initial commit as `stackmind init`.
                initialized = initialize(
                    project_path=workspace,
                    no_git=(state == "adopt"),
                    preserve_existing=(state == "adopt"),
                )
            except (RuntimeError, FileNotFoundError, OSError, UnicodeError) as exc:
                raise click.ClickException(str(exc)) from exc
            if not initialized:
                raise click.ClickException("StackMind initialization failed; TUI was not launched.")

    with StartupStep("Validating StackMind project...") as step:
        validation = validate(workspace, fix=False)
        safe_repairs = [issue for issue in validation.issues if _safe_generated_repair(issue)]
        if safe_repairs:
            from cli.validate import auto_fix

            auto_fix(workspace / ".sync", safe_repairs)
            validation = validate(workspace, fix=False)
        if validation.errors and _only_regenerable_graph_errors(validation.errors):
            try:
                build_knowledge_graph(workspace)
            except Exception as exc:
                raise click.ClickException(
                    f"Derived graph repair failed; TUI was not launched: {exc}"
                ) from exc
            validation = validate(workspace, fix=False)
        if validation.errors:
            details = "\n".join(f"- {issue.layer}: {issue.message}" for issue in validation.errors)
            raise click.ClickException(
                "Project validation failed; no automatic repair was attempted because "
                "the source-of-truth changes are ambiguous.\n"
                f"{details}\nRun `stackmind validate {workspace}` and repair the listed files."
            )
        if validation.warnings:
            step.set_complete_message(f"Validating StackMind project passed with {len(validation.warnings)} warning(s).")
        else:
            step.set_complete_message("Validating StackMind project passed.")

    with StartupStep("Checking knowledge graph...") as step:
        if graph_needs_build(workspace):
            step.set_message("Building initial or stale knowledge graph...")
            try:
                build_knowledge_graph(workspace)
            except Exception as exc:
                raise click.ClickException(
                    f"Knowledge graph preparation failed; TUI was not launched: {exc}"
                ) from exc
            step.set_complete_message("Knowledge graph is current.")
        else:
            step.set_complete_message("Knowledge graph is current.")

    # Record active project in ~/.stackmindcli/projects.json
    try:
        from validators.kernel.home import register_project
        register_project(workspace)
    except Exception:
        pass


def _safe_generated_repair(issue: Any) -> bool:
    """Allow only additive directory/.gitkeep repair paths from validate --fix."""
    if not issue.auto_fixable:
        return False
    message = issue.message
    return (
        "without .gitkeep" in message
        or ("Missing: .sync/inbox/" in message and "_read" in message)
        or "Missing: .sync/outbox/" in message
        or "Missing directory: .sync/" in message
    )


def _only_regenerable_graph_errors(issues: list[Any]) -> bool:
    """Accept only node projection defects; revisions and registry are authoritative."""
    if not issues:
        return False
    for issue in issues:
        if issue.layer != "Knowledge":
            return False
        if issue.path.startswith("knowledge/nodes/"):
            continue
        if not issue.path and issue.message.startswith("Live registry record lacks code node:"):
            continue
        return False
    return True


def graph_needs_build(workspace: Path) -> bool:
    """Return whether the persisted graph is missing, invalid, or stale."""
    from validators.knowledge.compiler import compile_project
    from validators.knowledge.storage import latest_revision_id, read_ir

    workspace = workspace.resolve()
    if latest_revision_id(workspace) == 0:
        return True
    try:
        persisted = read_ir(workspace)
    except (OSError, ValueError, KeyError, TypeError):
        # A malformed derived revision is safe to supersede through the normal
        # graph writer, provided project validation already passed.
        return True
    try:
        current = compile_project(workspace, agent="codex", write_registry=False)
    except Exception as exc:
        raise click.ClickException(f"Could not assess knowledge-graph freshness: {exc}") from exc

    return _graph_content(persisted) != _graph_content(current)


def _graph_content(ir: Any) -> tuple[str, str, str]:
    """Compare only source-derived content, not revision bookkeeping metadata."""
    return (
        _stable_items(ir.symbols),
        _stable_items(ir.edges),
        _stable_items(ir.diagnostics),
    )


def _stable_items(items: list[Any]) -> str:
    payloads = [json.dumps(item.to_dict(), sort_keys=True) for item in items]
    return json.dumps(sorted(payloads))


def build_knowledge_graph(workspace: Path) -> None:
    """Run the registered production graph-build command."""
    from cli.graph import graph

    command = graph.commands["build"]
    result = click.Context(command).invoke(
        command, project_path=str(workspace.resolve()), agent="codex"
    )
    if result is False:
        raise RuntimeError("graph build command reported failure")


def check_provider_configuration(workspace: Path) -> None:
    """Validate configured role/provider fields without probing or downloading."""
    with StartupStep(
        "Checking provider configuration...",
        complete_message="Provider configuration is ready (availability will be checked when used).",
    ):
        from validators.kernel.settings import (
            StackMindSettings,
            find_settings_file,
            get_default_settings,
        )

        settings_path = find_settings_file(workspace)
        if settings_path is None:
            settings = get_default_settings()
        else:
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                settings = StackMindSettings.from_dict(data)
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                AttributeError,
                TypeError,
                ValueError,
            ) as exc:
                raise click.ClickException(
                    f"Provider configuration at {settings_path} is unreadable: {exc}. "
                    "Repair it or configure providers from a healthy StackMind project."
                ) from exc
            defaults = get_default_settings()
            configured_roles = data.get("roles") if isinstance(data, dict) else None
            for name, default_provider in defaults.providers.items():
                provider = settings.providers.get(name)
                if provider is None:
                    settings.providers[name] = default_provider
                    continue
                if not provider.display_name:
                    provider.display_name = default_provider.display_name
                if not provider.base_url:
                    provider.base_url = default_provider.base_url
                if not provider.models:
                    provider.models = list(default_provider.models)
                if not provider.default_model:
                    provider.default_model = default_provider.default_model
            if not isinstance(configured_roles, dict):
                configured_roles = {}
            configured_role_names = set(configured_roles)
            if "code" in configured_role_names:
                configured_role_names.update(("backend", "frontend"))
            for role, default_role in defaults.roles.items():
                if role not in configured_role_names:
                    settings.roles[role] = dict(default_role)

        missing: list[str] = []
        for role, role_config in settings.roles.items():
            if not isinstance(role_config, dict):
                missing.append(f"role {role} has no provider mapping")
                continue
            provider_name = str(role_config.get("provider") or "").strip()
            model = str(role_config.get("model") or "").strip()
            provider = settings.providers.get(provider_name)
            if provider is None or not provider.enabled:
                missing.append(f"role {role} references an unconfigured provider")
            elif not model:
                missing.append(f"role {role} has no model configured")
            elif provider_name != "ollama" and not provider.api_key.strip():
                missing.append(f"role {role} is missing credentials for {provider_name}")
        if missing:
            details = "\n".join(f"- {item}" for item in missing)
            raise click.ClickException(
                "Provider configuration is incomplete (this is separate from provider "
                "or model availability):\n"
                f"{details}\nConfigure a provider and model, then retry."
            )


def ensure_daemon(workspace: Path, daemon_url: str | None, *, timeout: float = 8.0):
    """Reuse a healthy daemon or start one and wait for a real health response."""
    from cli.daemon import (
        DEFAULT_DAEMON_PORT,
        clear_daemon_pid,
        daemon_health,
        write_daemon_pid,
    )
    from validators.kernel.daemon import LocalDaemon

    workspace = workspace.resolve()
    url = daemon_url or f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"
    if daemon_health(url) is not None:
        with StartupStep("Connecting to StackMind daemon...", complete_message=f"Daemon ready at {url} (reused)."):
            pass
        return None, url
    if daemon_url:
        with StartupStep(f"Connecting to StackMind daemon at {url}...") as step:
            if _wait_for_health(url, timeout):
                step.set_complete_message(f"Daemon ready at {url} (reused).")
                return None, url
            raise click.ClickException(
                f"Configured daemon {url} did not become healthy within {timeout:g}s."
            )

    state_dir = workspace / ".sync" / "runtime" / "daemon"
    state_dir.mkdir(parents=True, exist_ok=True)
    with StartupStep("Starting StackMind daemon...") as step:
        try:
            daemon = LocalDaemon(str(state_dir), port=DEFAULT_DAEMON_PORT).start()
        except OSError:
            # The established TUI behavior falls back to an ephemeral local port
            # when the default port is occupied by an incompatible service.
            try:
                daemon = LocalDaemon(str(state_dir), port=0).start()
            except Exception as exc:
                raise click.ClickException(f"Could not start StackMind daemon: {exc}") from exc
        except Exception as exc:
            raise click.ClickException(f"Could not start StackMind daemon: {exc}") from exc
        url = daemon.url
        step.set_message(f"Starting StackMind daemon at {url}...")
        try:
            write_daemon_pid(workspace, daemon.address[1])
            if not _wait_for_health(url, timeout):
                if daemon.address[1] == DEFAULT_DAEMON_PORT:
                    daemon.stop()
                    daemon = LocalDaemon(str(state_dir), port=0).start()
                    url = daemon.url
                    step.set_message(f"Starting StackMind daemon at {url}...")
                    write_daemon_pid(workspace, daemon.address[1])
                    if not _wait_for_health(url, timeout):
                        raise click.ClickException(
                            f"StackMind daemon did not report healthy at {url} within {timeout:g}s."
                        )
                else:
                    raise click.ClickException(
                        f"StackMind daemon did not report healthy at {url} within {timeout:g}s."
                    )
        except BaseException:
            # BaseException so Ctrl+C (KeyboardInterrupt) still stops the owned
            # daemon and clears the pid file instead of leaking the process.
            try:
                daemon.stop()
            finally:
                clear_daemon_pid(workspace, expected_pid=os.getpid())
            raise
        step.set_complete_message(f"Daemon ready at {url} (started for this TUI).")
        return daemon, url


def _wait_for_health(url: str, timeout: float) -> bool:
    from cli.daemon import daemon_health

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_health(url) is not None:
            return True
        time.sleep(0.1)
    return daemon_health(url) is not None
