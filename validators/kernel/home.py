"""Centralized storage and configuration management for StackMind CLI.

Provides ~/.stackmindcli as the single authoritative root for:
- Global settings & provider configurations (settings.json)
- Projects registry & metadata (projects.json)
- Global procedural skill repository (skills/global, skills/cache)
- Central daemon runtime & coordination (daemon/)
- Central persistent sessions & transcripts (sessions/)
- Shared scratch space, diff cache, and temporary sandboxes (tmp/)

Handles seamless backward-compatibility and migration from legacy ~/.stackmind.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STACKMIND_HOME_ENV = "STACKMIND_HOME"
LEGACY_STACKMIND_DIR_NAME = ".stackmind"
STACKMIND_CLI_DIR_NAME = ".stackmindcli"


def get_stackmind_home() -> Path:
    """Return the authoritative ~/.stackmindcli root directory.

    Respects STACKMIND_HOME environment variable if set.
    Creates required root directories and performs one-time migration
    from legacy ~/.stackmind if present.
    """
    override = os.environ.get(STACKMIND_HOME_ENV)
    if override:
        home_path = Path(override).expanduser().resolve()
    else:
        home_path = (Path.home() / STACKMIND_CLI_DIR_NAME).resolve()

    try:
        home_path.mkdir(parents=True, exist_ok=True)
        # Subdirectories
        for subdir in ("skills/global", "skills/cache", "daemon", "sessions", "tmp"):
            (home_path / subdir).mkdir(parents=True, exist_ok=True)
        # Ensure global settings exist in both target home and ~/.stackmind/
        ensure_global_settings_file(home_path)
    except (PermissionError, OSError):
        pass

    return home_path


def ensure_global_settings_file(target_home: Path | None = None) -> Path:
    """Ensure settings.json exists in target home and ~/.stackmind/ with default values."""
    cli_home = (target_home or get_stackmind_home()).resolve()
    legacy_home = (Path.home() / LEGACY_STACKMIND_DIR_NAME).resolve()

    target_settings = cli_home / "settings.json"
    legacy_settings = legacy_home / "settings.json"

    # 1. Check if legacy settings already exist, copy to cli_home
    if not target_settings.is_file() and legacy_settings.is_file():
        try:
            shutil.copy2(legacy_settings, target_settings)
        except Exception:
            pass

    # 2. If neither exists, generate default settings content
    try:
        from validators.kernel.settings import get_default_settings
        default_text = get_default_settings().to_json(indent=2)
    except Exception:
        import json
        default_text = json.dumps({"execution_profile": "medium", "providers": {}, "roles": {}}, indent=2)

    content_to_write = default_text
    if target_settings.is_file():
        try:
            raw_text = target_settings.read_text(encoding="utf-8").strip()
            if raw_text and raw_text != '{\n  "providers": {},\n  "roles": {}\n}':
                content_to_write = raw_text
            else:
                content_to_write = default_text
                target_settings.write_text(default_text, encoding="utf-8")
        except Exception:
            content_to_write = default_text
    elif legacy_settings.is_file():
        try:
            raw_text = legacy_settings.read_text(encoding="utf-8").strip()
            if raw_text and raw_text != '{\n  "providers": {},\n  "roles": {}\n}':
                content_to_write = raw_text
            else:
                content_to_write = default_text
        except Exception:
            content_to_write = default_text
    else:
        try:
            cli_home.mkdir(parents=True, exist_ok=True)
            target_settings.write_text(default_text, encoding="utf-8")
        except Exception:
            pass

    # 3. Mirror into ~/.stackmind/ and alias setting.json in both
    for base_dir in (cli_home, legacy_home):
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            s_plural = base_dir / "settings.json"
            if not s_plural.is_file() or s_plural.read_text(encoding="utf-8").strip() == '{\n  "providers": {},\n  "roles": {}\n}':
                s_plural.write_text(content_to_write, encoding="utf-8")
            s_singular = base_dir / "setting.json"
            if not s_singular.is_file() or s_singular.read_text(encoding="utf-8").strip() == '{\n  "providers": {},\n  "roles": {}\n}':
                s_singular.write_text(content_to_write, encoding="utf-8")
        except Exception:
            pass

    return target_settings


def _migrate_legacy_settings_if_needed(target_home: Path) -> None:
    """Migrate settings from legacy ~/.stackmind/ to target home if missing."""
    ensure_global_settings_file(target_home)


def get_global_settings_path() -> Path:
    """Return canonical path to ~/.stackmindcli/settings.json."""
    return get_stackmind_home() / "settings.json"


def get_projects_registry_path() -> Path:
    """Return canonical path to ~/.stackmindcli/projects.json."""
    return get_stackmind_home() / "projects.json"


def get_global_skills_dir() -> Path:
    """Return directory containing global procedural skills (~/.stackmindcli/skills)."""
    return get_stackmind_home() / "skills"


def get_global_tmp_dir() -> Path:
    """Return directory for shared scratch spaces and temp files (~/.stackmindcli/tmp)."""
    d = get_stackmind_home() / "tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass
    return d


def get_daemon_home_dir() -> Path:
    """Return directory for central daemon runtime (~/.stackmindcli/daemon)."""
    d = get_stackmind_home() / "daemon"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass
    return d


def get_sessions_home_dir() -> Path:
    """Return directory for central session storage (~/.stackmindcli/sessions)."""
    d = get_stackmind_home() / "sessions"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass
    return d


# ── Projects Registry Operations ─────────────────────────────────────────────


def _read_projects_registry() -> dict[str, Any]:
    """Read the projects.json registry file safely."""
    path = get_projects_registry_path()
    if not path.is_file():
        return {"version": "1.0", "updated_at": None, "projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "projects" in data and isinstance(data["projects"], dict):
            return data
    except Exception:
        pass
    return {"version": "1.0", "updated_at": None, "projects": {}}


def _write_projects_registry(data: dict[str, Any]) -> None:
    """Write projects.json atomically."""
    path = get_projects_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def register_project(
    project_path: Path | str,
    name: str | None = None,
    agents: list[str] | None = None,
) -> dict[str, Any]:
    """Register or update a project in the central projects registry.

    Args:
        project_path: Path to the StackMind project root.
        name: Optional display name for the project.
        agents: Optional list of configured agent roles.

    Returns:
        The registered project metadata record.
    """
    resolved = Path(project_path).resolve()
    key = resolved.as_posix()
    now_iso = datetime.now(timezone.utc).isoformat()

    registry = _read_projects_registry()
    projects = registry.setdefault("projects", {})

    existing = projects.get(key, {})
    reg_at = existing.get("registered_at") or now_iso

    record: dict[str, Any] = {
        "name": name or existing.get("name") or resolved.name,
        "path": str(resolved),
        "posix_path": key,
        "registered_at": reg_at,
        "last_accessed": now_iso,
        "agents": agents or existing.get("agents") or ["claude", "codex", "gemini", "gemma", "local-llm"],
        "has_sync": (resolved / ".sync").is_dir(),
        "status": "active",
    }
    projects[key] = record
    _write_projects_registry(registry)
    return record


def unregister_project(project_path: Path | str) -> bool:
    """Remove a project from the central projects registry.

    Returns:
        True if the project was found and removed, False otherwise.
    """
    resolved = Path(project_path).resolve()
    key = resolved.as_posix()

    registry = _read_projects_registry()
    projects = registry.get("projects", {})
    if key in projects:
        del projects[key]
        _write_projects_registry(registry)
        return True
    return False


def touch_project(project_path: Path | str) -> None:
    """Update the last_accessed timestamp for a registered project."""
    resolved = Path(project_path).resolve()
    key = resolved.as_posix()

    registry = _read_projects_registry()
    projects = registry.get("projects", {})
    if key in projects:
        projects[key]["last_accessed"] = datetime.now(timezone.utc).isoformat()
        projects[key]["has_sync"] = (resolved / ".sync").is_dir()
        _write_projects_registry(registry)


def get_project_entry(project_path: Path | str) -> dict[str, Any] | None:
    """Retrieve metadata for a specific registered project."""
    resolved = Path(project_path).resolve()
    key = resolved.as_posix()
    registry = _read_projects_registry()
    return registry.get("projects", {}).get(key)


def list_registered_projects() -> list[dict[str, Any]]:
    """List all registered projects sorted by last_accessed descending."""
    registry = _read_projects_registry()
    projects = list(registry.get("projects", {}).values())

    # Refresh has_sync status
    for p in projects:
        p_path = Path(p["path"])
        p["exists"] = p_path.is_dir()
        p["has_sync"] = (p_path / ".sync").is_dir()

    return sorted(
        projects,
        key=lambda p: str(p.get("last_accessed") or p.get("registered_at") or ""),
        reverse=True,
    )


__all__ = [
    "STACKMIND_CLI_DIR_NAME",
    "STACKMIND_HOME_ENV",
    "get_daemon_home_dir",
    "get_global_settings_path",
    "get_global_skills_dir",
    "get_global_tmp_dir",
    "get_project_entry",
    "get_projects_registry_path",
    "get_sessions_home_dir",
    "get_stackmind_home",
    "list_registered_projects",
    "register_project",
    "touch_project",
    "unregister_project",
]
