"""Tests for the StackMind symbol registry and knowledge validation layer."""

import json
import shutil

import pytest

from cli.init import init
from cli.lock import acquire_lock, release_lock
from cli.validate import validate
from validators.knowledge.registry import (
    RegistryLockError,
    SymbolRegistry,
    birth_key,
    node_id_for,
)


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


@pytest.fixture
def registry(fresh_project):
    return SymbolRegistry(fresh_project, agent="codex")


def test_node_id_is_deterministic_and_uses_16_hex():
    key = birth_key("src/auth.py", "AuthService.login")
    first = node_id_for("Function", key)
    second = node_id_for("Function", key)

    assert first == second
    assert first.startswith("FUNC-")
    assert len(first.split("-", 1)[1]) == 16
    int(first.split("-", 1)[1], 16)


def test_registry_writes_require_lock(registry):
    with pytest.raises(RegistryLockError):
        registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="AuthService.login",
        )


def test_create_if_missing_writes_sharded_record(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        written = registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="AuthService.login",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    shard = written.node_id.split("-", 1)[1][:2]
    expected_path = fresh_project / ".sync" / "knowledge" / "registry" / shard / f"{written.node_id}.json"
    assert written.path == expected_path
    assert expected_path.exists()

    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert data["node_id"] == written.node_id
    assert data["birth_key"] == "src/auth.py:AuthService.login"
    assert data["history"][0]["key"] == data["birth_key"]


def test_same_repo_compiled_twice_produces_identical_node_ids(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        first = registry.get_or_create(
            kind="Class",
            path="src/auth.py",
            qualified_name="AuthService",
        )
        second = registry.get_or_create(
            kind="Class",
            path="src/auth.py",
            qualified_name="AuthService",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    assert first.node_id == second.node_id


def test_registry_survives_deletion_of_derived_projections(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        written = registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="login",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    derived = fresh_project / ".sync" / "knowledge" / "nodes"
    derived.mkdir(parents=True)
    (derived / "FUNC-derived.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(derived)

    assert registry.load(written.node_id)["node_id"] == written.node_id


def test_stackmind_validate_layer5_passes_on_seeded_registry(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="login",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    result = validate(fresh_project)
    assert result.passed
    assert not [issue for issue in result.issues if issue.layer == "Knowledge"]


def test_stackmind_validate_layer5_fails_on_duplicate_id(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        written = registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="login",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    duplicate_dir = fresh_project / ".sync" / "knowledge" / "registry" / "ff"
    duplicate_dir.mkdir(parents=True)
    duplicate = dict(written.record)
    duplicate["current"] = {
        "path": "src/other.py",
        "qualified_name": "login",
    }
    (duplicate_dir / "duplicate.json").write_text(
        json.dumps(duplicate, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    result = validate(fresh_project)
    assert not result.passed
    assert any("Duplicate NodeID" in issue.message for issue in result.errors)


def test_stackmind_validate_layer5_fails_on_birth_hash_mismatch(fresh_project, registry):
    acquire_lock(fresh_project / ".sync", "codex", session_id=1)
    try:
        written = registry.get_or_create(
            kind="Function",
            path="src/auth.py",
            qualified_name="login",
        )
    finally:
        release_lock(fresh_project / ".sync", "codex")

    path = written.path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["history"][0]["key"] = "src/other.py:login"
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    result = validate(fresh_project)
    assert not result.passed
    assert any("birth-hash mismatch" in issue.message for issue in result.errors)
