"""Comprehensive test suite for StackMind Skill Consumer and :consume-skills command."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from cli.tui.commands import dispatch_delivery_command
from cli.tui.state import AutonomousDeliveryState
from validators.kernel.home import STACKMIND_HOME_ENV, get_global_skills_dir, get_stackmind_home
from validators.skill.consumer import (
    consume_skills,
    parse_frontmatter,
    parse_skill_file,
    parse_skill_json,
    parse_skill_markdown,
    parse_skill_yaml,
    scan_skill_directory,
    slugify_skill_name,
)
from validators.skill.models import RiskTier, SkillRecord, SkillStatus
from validators.skill.store import SkillStore


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate STACKMIND_HOME to a temporary directory."""
    home_dir = tmp_path / "central_home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(home_dir))
    return home_dir


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary initialized project workspace."""
    project = tmp_path / "consumer-test-workspace"
    init(project, name="Consumer Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def test_slugify_skill_name():
    assert slugify_skill_name("My Cool Skill") == "my_cool_skill"
    assert slugify_skill_name("Deploy-To-K8s!") == "deploy-to-k8s"
    assert slugify_skill_name("123_number_skill") == "123_number_skill"
    assert slugify_skill_name("  --already-slugged--  ") == "already-slugged"
    assert slugify_skill_name("!@#$%^") == "unnamed_skill"


def test_parse_frontmatter():
    content = """---
name: sample-skill
description: A sample skill description
risk_tier: low
---
# Sample Skill
1. Do something
"""
    fm, body = parse_frontmatter(content)
    assert fm.get("name") == "sample-skill"
    assert fm.get("risk_tier") == "low"
    assert "# Sample Skill" in body


def test_parse_skill_markdown_with_frontmatter(tmp_path):
    md_content = """---
name: deploy-preview
description: Procedure for deploying a preview environment to staging
risk_tier: medium
preconditions:
  - Docker daemon is running
  - AWS credentials configured
target_modules:
  - infra/*
  - deploy/*
author: test-author
---
# Deploy Preview Environment

Use this procedural runbook when deploying previews.

## Steps
1. **Build container**: Build the preview container image
```bash
docker build -t app:preview .
```
2. **Push to registry**: Push the tagged image to ECR
```bash
docker push app:preview
```
3. **Trigger helm deploy**: Deploy helm release to staging cluster
```bash
helm upgrade --install preview ./helm -f values-preview.yaml
```
"""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(md_content, encoding="utf-8")

    rec = parse_skill_file(skill_file)
    assert rec.name == "deploy-preview"
    assert rec.version == 1
    assert rec.risk_tier == RiskTier.MEDIUM
    assert rec.status == SkillStatus.ACTIVE
    assert "preview environment" in rec.description.lower()
    assert len(rec.steps) == 3

    step1 = rec.steps[0]
    assert step1.step_index == 1
    assert step1.action == "Build container"
    assert step1.tool == "bash"
    assert step1.command_template == "docker build -t app:preview ."

    step2 = rec.steps[1]
    assert step2.step_index == 2
    assert step2.action == "Push to registry"
    assert step2.tool == "bash"

    step3 = rec.steps[2]
    assert step3.step_index == 3
    assert step3.action == "Trigger helm deploy"
    assert step3.tool == "bash"

    errs = rec.validate_schema()
    assert not errs, f"Validation errors: {errs}"


def test_parse_skill_markdown_without_frontmatter(tmp_path):
    md_content = """# Cache Cleanup Procedure

Utility skill to purge temporary build caches and pycache artifacts.

1. Clean python bytecode
```bash
find . -name "*.pyc" -delete
```
2. Remove pytest cache
```bash
rm -rf .pytest_cache
```
"""
    skill_file = tmp_path / "cleanup.md"
    skill_file.write_text(md_content, encoding="utf-8")

    rec = parse_skill_file(skill_file)
    assert rec.name == "cache_cleanup_procedure"
    assert rec.risk_tier == RiskTier.LOW
    assert len(rec.steps) == 2
    assert rec.steps[0].action == "Clean python bytecode"
    assert rec.steps[1].action == "Remove pytest cache"

    errs = rec.validate_schema()
    assert not errs, f"Validation errors: {errs}"


def test_parse_skill_json_simplified(tmp_path):
    json_content = {
        "name": "lint_and_format",
        "description": "Run black and ruff linters across codebase",
        "risk_tier": "low",
        "steps": [
            {"action": "Run Ruff check", "command": "ruff check .", "tool": "bash"},
            {"action": "Run Black format", "command": "black --check .", "tool": "bash"},
        ],
    }
    skill_file = tmp_path / "lint.json"
    skill_file.write_text(json.dumps(json_content), encoding="utf-8")

    rec = parse_skill_file(skill_file)
    assert rec.name == "lint_and_format"
    assert rec.risk_tier == RiskTier.LOW
    assert len(rec.steps) == 2
    assert rec.steps[0].action == "Run Ruff check"
    assert rec.steps[0].command_template == "ruff check ."

    errs = rec.validate_schema()
    assert not errs, f"Validation errors: {errs}"


def test_parse_skill_yaml(tmp_path):
    yaml_content = """
name: test_suite_runner
description: Execute full test suite with coverage
risk_tier: low
steps:
  - action: Run pytest with coverage
    command: pytest --cov=src tests/
    tool: bash
"""
    skill_file = tmp_path / "test.yaml"
    skill_file.write_text(yaml_content, encoding="utf-8")

    rec = parse_skill_file(skill_file)
    assert rec.name == "test_suite_runner"
    assert len(rec.steps) == 1
    assert rec.steps[0].action == "Run pytest with coverage"

    errs = rec.validate_schema()
    assert not errs, f"Validation errors: {errs}"


def test_scan_skill_directory(tmp_path):
    # Setup multiple skills under directory
    pkg_dir = tmp_path / "skills_bundle"
    pkg_dir.mkdir()

    s1_dir = pkg_dir / "skill_one"
    s1_dir.mkdir()
    (s1_dir / "SKILL.md").write_text(
        "---\nname: skill_one\ndescription: First skill\n---\n# Skill One\n1. Step one\n",
        encoding="utf-8",
    )

    s2_dir = pkg_dir / "skill_two"
    s2_dir.mkdir()
    (s2_dir / "SKILL.md").write_text(
        "---\nname: skill_two\ndescription: Second skill\n---\n# Skill Two\n1. Step two\n",
        encoding="utf-8",
    )

    discovered = scan_skill_directory(pkg_dir)
    discovered_names = {s.name for s in discovered}
    assert "skill_one" in discovered_names
    assert "skill_two" in discovered_names
    assert len(discovered) == 2


def test_consume_skills_global_storage(isolated_home, tmp_path):
    skill_file = tmp_path / "global_skill.md"
    skill_file.write_text(
        "---\nname: global_helper\ndescription: A globally available procedural skill\n---\n# Global Helper\n1. Help user\n",
        encoding="utf-8",
    )

    consumed = consume_skills(skill_file, is_global=True)
    assert len(consumed) == 1
    assert consumed[0].name == "global_helper"

    # Check file exists in ~/.stackmindcli/skills/global/global_helper.json
    expected_path = get_global_skills_dir() / "global" / "global_helper.json"
    assert expected_path.is_file()

    # Verify SkillStore finds it
    store = SkillStore()
    retrieved = store.get_skill("global_helper")
    assert retrieved is not None
    assert retrieved.name == "global_helper"
    assert retrieved.status == SkillStatus.ACTIVE


def test_consume_skills_local_storage(isolated_home, temp_workspace, tmp_path):
    skill_file = tmp_path / "local_skill.md"
    skill_file.write_text(
        "---\nname: local_helper\ndescription: A project-specific procedural skill\n---\n# Local Helper\n1. Local task\n",
        encoding="utf-8",
    )

    consumed = consume_skills(skill_file, project_path=temp_workspace, is_global=False)
    assert len(consumed) == 1
    assert consumed[0].name == "local_helper"

    # Check file exists in project's active directory
    active_path = temp_workspace / ".sync" / "skills" / "active" / "local_helper.json"
    manifest_path = temp_workspace / ".sync" / "skills" / "manifests" / "local_helper" / "v1.json"
    assert active_path.is_file()
    assert manifest_path.is_file()

    # SkillStore for workspace finds it
    store = SkillStore(temp_workspace)
    retrieved = store.get_skill("local_helper")
    assert retrieved is not None
    assert retrieved.name == "local_helper"


def test_tui_dispatch_consume_skills_no_args():
    from unittest.mock import MagicMock
    adapter = MagicMock()
    client = MagicMock()
    session = {"session_id": "test-sid", "workspace": "."}
    state = AutonomousDeliveryState()

    _, is_prompt = dispatch_delivery_command(adapter, client, session, ":consume-skills", state)
    assert is_prompt is False
    assert len(state.messages) >= 2
    last_msg = state.messages[-1].content
    assert "CONSUME PROCEDURAL SKILLS" in last_msg
    assert "Usage:" in last_msg
    assert "Supported Formats:" in last_msg


def test_tui_dispatch_consume_skills_with_file(isolated_home, tmp_path):
    from unittest.mock import MagicMock
    adapter = MagicMock()
    client = MagicMock()
    skill_file = tmp_path / "tui_skill.md"
    skill_file.write_text(
        "---\nname: tui_skill\ndescription: Skill consumed via TUI\n---\n# TUI Skill\n1. Step A\n2. Step B\n",
        encoding="utf-8",
    )

    session = {"session_id": "test-sid", "workspace": str(tmp_path)}
    state = AutonomousDeliveryState()

    cmd = f":consume-skills {skill_file}"
    _, is_prompt = dispatch_delivery_command(adapter, client, session, cmd, state)
    assert is_prompt is False
    last_msg = state.messages[-1].content
    assert "Successfully installed 1 skill(s)" in last_msg
    assert "tui_skill" in last_msg

    # Verify global store has it
    store = SkillStore()
    assert store.get_skill("tui_skill") is not None


def test_tui_dispatch_projects(isolated_home):
    from unittest.mock import MagicMock
    adapter = MagicMock()
    client = MagicMock()
    session = {"session_id": "test-sid", "workspace": "."}
    state = AutonomousDeliveryState()

    _, is_prompt = dispatch_delivery_command(adapter, client, session, ":projects", state)
    assert is_prompt is False
    last_msg = state.messages[-1].content
    assert "REGISTERED STACKMIND PROJECTS" in last_msg


def test_cli_skill_consume_and_install_commands(isolated_home, tmp_path):
    runner = CliRunner()
    skill_file = tmp_path / "cli_skill.md"
    skill_file.write_text(
        "---\nname: cli_skill\ndescription: Skill consumed via CLI\n---\n# CLI Skill\n1. Do CLI work\n",
        encoding="utf-8",
    )

    # 1. Test `stackmind skill consume`
    res1 = runner.invoke(cli, ["skill", "consume", str(skill_file)])
    assert res1.exit_code == 0
    assert "Successfully consumed 1 procedural skill(s)" in res1.output
    assert "cli_skill" in res1.output

    # 2. Test `stackmind skill install` alias
    skill_file2 = tmp_path / "cli_skill_2.md"
    skill_file2.write_text(
        "---\nname: cli_skill_two\ndescription: Second CLI skill\n---\n# CLI Skill Two\n1. Do CLI work 2\n",
        encoding="utf-8",
    )
    res2 = runner.invoke(cli, ["skill", "install", str(skill_file2)])
    assert res2.exit_code == 0
    assert "Successfully consumed 1 procedural skill(s)" in res2.output
    assert "cli_skill_two" in res2.output


def test_is_url():
    from validators.skill.consumer import is_url

    assert is_url("https://github.com/stackmind/skills")
    assert is_url("http://example.com/skill.md")
    assert is_url("git@github.com:stackmind/skills.git")
    assert is_url("github.com/stackmind/skills")
    assert is_url("https://raw.githubusercontent.com/stackmind/skills/main/SKILL.md")
    assert not is_url("C:\\path\\to\\SKILL.md")
    assert not is_url("/var/skills/SKILL.md")
    assert not is_url("local_skill.md")


def test_fetch_skills_from_raw_url_mocked(monkeypatch):
    import io
    from unittest.mock import MagicMock
    from validators.skill.consumer import fetch_skills_from_url

    fake_skill_content = """---
name: github-remote-skill
description: Fetched from GitHub raw endpoint
risk_tier: low
---
# Remote Skill
1. Step one from remote
```bash
echo hello
```
"""
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return fake_skill_content.encode("utf-8")

    mock_urlopen = MagicMock(return_value=FakeResponse())
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    # 1. Test raw URL
    records = fetch_skills_from_url("https://raw.githubusercontent.com/owner/repo/main/SKILL.md")
    assert len(records) == 1
    assert records[0].name == "github-remote-skill"
    assert len(records[0].steps) == 1
    assert records[0].steps[0].command_template == "echo hello"

    # 2. Test GitHub blob URL conversion
    records_blob = fetch_skills_from_url("https://github.com/owner/repo/blob/main/skills/SKILL.md")
    assert len(records_blob) == 1
    assert records_blob[0].name == "github-remote-skill"


def test_consume_skills_from_github_url_mocked(isolated_home, monkeypatch):
    from unittest.mock import MagicMock
    from validators.skill.consumer import consume_skills

    fake_skill_content = """---
name: remote-ci-cd
description: Continuous integration runbook from GitHub
risk_tier: medium
---
# CI CD Runbook
1. Run test suite
```bash
pytest
```
"""
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return fake_skill_content.encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=FakeResponse()))

    # Consume directly using a GitHub URL
    saved = consume_skills("https://github.com/example-org/repo/blob/main/skills/ci.md", is_global=True)
    assert len(saved) == 1
    assert saved[0].name == "remote-ci-cd"

    # Verify it persisted to global store
    store = SkillStore()
    retrieved = store.get_skill("remote-ci-cd")
    assert retrieved is not None
    assert retrieved.name == "remote-ci-cd"
    assert "Continuous integration" in retrieved.description


def test_parse_skill_with_semver_version():
    """Verify that skills with semver strings (e.g. '0.4.0' or 'v2') parse cleanly without ValueError."""
    semver_content = """---
name: asd-ste100
description: "ASD-STE100 Simplified Technical English"
version: 0.4.0
risk_tier: low
---
# Simplified Technical English
1. Write in active voice
2. Limit instructions to one sentence
"""
    rec = parse_skill_markdown(semver_content)
    assert rec.name == "asd-ste100"
    assert rec.version == 1
    assert len(rec.steps) == 2

    # String version in JSON manifest
    json_manifest = {
        "name": "semver_json_skill",
        "version": "2.1.0",
        "description": "Semver in JSON",
        "steps": ["Step 1", "Step 2"],
    }
    rec_json = parse_skill_json(json_manifest)
    assert rec_json.name == "semver_json_skill"
    assert rec_json.version == 2
    assert len(rec_json.steps) == 2


def test_consume_skills_github_clone_fallback_to_raw(isolated_home, monkeypatch):
    """Verify that if git clone fails, fetch_skills_from_url falls back to downloading raw SKILL.md."""
    import subprocess
    from unittest.mock import MagicMock
    from validators.skill.consumer import fetch_skills_from_url

    raw_skill_content = """---
name: asd-ste100
description: Simplified Technical English
version: 0.4.0
---
# Simplified Technical English
1. Active voice
"""
    class FakeRawResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return raw_skill_content.encode("utf-8")

    # Force git clone to raise CalledProcessError
    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git", "clone"], stderr="Network unreachable")

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=FakeRawResponse()))

    skills = fetch_skills_from_url("https://github.com/danyuchn/asd-ste100-skill")
    assert len(skills) == 1
    assert skills[0].name == "asd-ste100"
    assert skills[0].version == 1

