"""Tests for centralized ~/.stackmindcli directory structure, registry, and global skills (Phase 1)."""

import json
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli
from validators.kernel.home import (
    STACKMIND_HOME_ENV,
    get_daemon_home_dir,
    get_global_settings_path,
    get_global_skills_dir,
    get_global_tmp_dir,
    get_project_entry,
    get_projects_registry_path,
    get_sessions_home_dir,
    get_stackmind_home,
    list_registered_projects,
    register_project,
    touch_project,
    unregister_project,
)
from validators.kernel.settings import find_settings_file
from validators.skill.models import RiskTier, SkillRecord, SkillStatus, SkillStep
from validators.skill.store import SkillStore


def test_get_stackmind_home_creates_directories(monkeypatch, tmp_path: Path):
    """Verify get_stackmind_home respects STACKMIND_HOME and provisions subdirs."""
    custom_home = tmp_path / "custom_cli_home"
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    home = get_stackmind_home()
    assert home == custom_home.resolve()
    assert home.is_dir()
    assert (home / "skills" / "global").is_dir()
    assert (home / "skills" / "cache").is_dir()
    assert (home / "daemon").is_dir()
    assert (home / "sessions").is_dir()
    assert (home / "tmp").is_dir()
    assert get_global_settings_path() == home / "settings.json"
    assert get_projects_registry_path() == home / "projects.json"
    assert get_global_tmp_dir() == home / "tmp"
    assert get_daemon_home_dir() == home / "daemon"
    assert get_sessions_home_dir() == home / "sessions"


def test_legacy_settings_migration(monkeypatch, tmp_path: Path):
    """Verify settings from legacy ~/.stackmind/ are copied to ~/.stackmindcli/."""
    custom_home = tmp_path / ".stackmindcli"
    fake_user_home = tmp_path / "user_home"
    legacy_dir = fake_user_home / ".stackmind"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_settings = legacy_dir / "settings.json"
    legacy_settings.write_text('{"execution_profile": "high"}', encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_user_home)
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    home = get_stackmind_home()
    target_settings = home / "settings.json"
    assert target_settings.is_file()
    assert json.loads(target_settings.read_text(encoding="utf-8")) == {"execution_profile": "high"}


def test_find_settings_file_prioritizes_stackmindcli(monkeypatch, tmp_path: Path):
    """Verify find_settings_file prefers ~/.stackmindcli/settings.json over legacy ~/.stackmind/."""
    custom_home = tmp_path / ".stackmindcli"
    custom_home.mkdir(parents=True, exist_ok=True)
    cli_settings = custom_home / "settings.json"
    cli_settings.write_text('{"source": "stackmindcli"}', encoding="utf-8")

    fake_user_home = tmp_path / "user_home"
    legacy_dir = fake_user_home / ".stackmind"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "settings.json").write_text('{"source": "legacy"}', encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_user_home)
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    # In an empty workspace without local settings
    empty_ws = tmp_path / "workspace"
    empty_ws.mkdir()

    found = find_settings_file(empty_ws)
    assert found == cli_settings
    assert json.loads(found.read_text(encoding="utf-8")) == {"source": "stackmindcli"}


def test_projects_registry_lifecycle(monkeypatch, tmp_path: Path):
    """Verify registering, listing, touching, and unregistering projects."""
    custom_home = tmp_path / ".stackmindcli"
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    proj_a = tmp_path / "project_alpha"
    proj_a.mkdir()
    (proj_a / ".sync").mkdir()

    proj_b = tmp_path / "project_beta"
    proj_b.mkdir()

    # 1. Register projects
    rec_a = register_project(proj_a, name="Alpha Project")
    assert rec_a["name"] == "Alpha Project"
    assert rec_a["has_sync"] is True

    rec_b = register_project(proj_b)
    assert rec_b["name"] == "project_beta"
    assert rec_b["has_sync"] is False

    # 2. List projects
    projs = list_registered_projects()
    assert len(projs) == 2
    names = {p["name"] for p in projs}
    assert "Alpha Project" in names
    assert "project_beta" in names

    # 3. Touch project
    touch_project(proj_a)
    entry_a = get_project_entry(proj_a)
    assert entry_a is not None
    assert entry_a["name"] == "Alpha Project"

    # 4. Unregister project
    assert unregister_project(proj_b) is True
    assert unregister_project(proj_b) is False
    assert len(list_registered_projects()) == 1


def test_skill_store_global_library_integration(monkeypatch, tmp_path: Path):
    """Verify SkillStore discovers and saves skills in centralized global library."""
    custom_home = tmp_path / ".stackmindcli"
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    (project_dir / ".sync").mkdir()

    store = SkillStore(project_dir)

    from validators.skill.models import SkillApplicability, SkillProvenance

    # Create a local skill
    local_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("local-test-runner", 1),
        name="local-test-runner",
        version=1,
        status=SkillStatus.ACTIVE,
        risk_tier=RiskTier.LOW,
        description="Local runner",
        applicability=SkillApplicability(),
        steps=(SkillStep(step_index=1, action="Run tests", tool="bash", command_template="pytest"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-10-13T00:00:00Z", author_agent="codex"),
    )
    store.save_version(local_skill)

    # Save a global skill directly to ~/.stackmindcli/skills/global
    global_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("global-python-formatter", 1),
        name="global-python-formatter",
        version=1,
        status=SkillStatus.ACTIVE,
        risk_tier=RiskTier.LOW,
        description="Global formatter",
        applicability=SkillApplicability(),
        steps=(SkillStep(step_index=1, action="Format python", tool="bash", command_template="black ."),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-10-13T00:00:00Z", author_agent="codex"),
    )
    saved_path = store.save_global_skill(global_skill)
    assert saved_path.is_file()
    assert str(custom_home) in str(saved_path)

    # 1. get_skill finds both local and global
    assert store.get_skill("local-test-runner") is not None
    assert store.get_skill("global-python-formatter") is not None

    # 2. list_skills includes both local and global skills
    all_skills = store.list_skills(active_only=True)
    skill_names = {s.name for s in all_skills}
    assert "local-test-runner" in skill_names
    assert "global-python-formatter" in skill_names


def test_cli_projects_commands(monkeypatch, tmp_path: Path):
    """Verify `stackmind projects` CLI commands."""
    custom_home = tmp_path / ".stackmindcli"
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    sample_proj = tmp_path / "demo_app"
    sample_proj.mkdir()
    (sample_proj / ".sync").mkdir()

    runner = CliRunner()

    # 1. stackmind projects home
    res_home = runner.invoke(cli, ["projects", "home"])
    assert res_home.exit_code == 0
    assert ".stackmindcli" in res_home.output

    # 2. stackmind projects register
    res_reg = runner.invoke(cli, ["projects", "register", str(sample_proj), "--name", "Demo App"])
    assert res_reg.exit_code == 0
    assert "Registered project: Demo App" in res_reg.output

    # 3. stackmind projects list
    res_list = runner.invoke(cli, ["projects", "list"])
    assert res_list.exit_code == 0
    assert "Demo App" in res_list.output
    assert ".sync" in res_list.output

    # 4. stackmind projects unregister
    res_unreg = runner.invoke(cli, ["projects", "unregister", str(sample_proj)])
    assert res_unreg.exit_code == 0
    assert "Unregistered project" in res_unreg.output

    # 5. stackmind projects list is now empty
    res_empty = runner.invoke(cli, ["projects", "list"])
    assert res_empty.exit_code == 0
    assert "No projects registered" in res_empty.output


def test_auto_provision_global_settings_and_mirroring(monkeypatch, tmp_path: Path):
    """Verify settings.json and setting.json are automatically provisioned in both ~/.stackmindcli and ~/.stackmind."""
    custom_home = tmp_path / ".stackmindcli"
    fake_user_home = tmp_path / "user_home"
    monkeypatch.setattr(Path, "home", lambda: fake_user_home)
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(custom_home))

    # Before calling get_stackmind_home, neither directory has settings
    home = get_stackmind_home()

    # Both ~/.stackmindcli and ~/.stackmind have settings.json and setting.json
    legacy_home = fake_user_home / ".stackmind"
    assert (home / "settings.json").is_file()
    assert (home / "setting.json").is_file()
    assert (legacy_home / "settings.json").is_file()
    assert (legacy_home / "setting.json").is_file()

    # Content is valid JSON
    data = json.loads((legacy_home / "settings.json").read_text(encoding="utf-8"))
    assert "providers" in data
    assert "roles" in data
