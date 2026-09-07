"""Tests for stackmind migrate command."""

import shutil
from pathlib import Path

import pytest
import yaml

from cli.init import init
from cli.migrate import (
    _execute_action,
    _resolve_targets,
    apply_migration,
    get_applied_migrations,
    get_current_version,
    get_pending_migrations,
    load_migrations,
    migrate,
    rollback_migration,
    save_applied_migrations,
)
from cli.validate import validate


@pytest.fixture
def fresh_project(tmp_path):
    """Create a fresh valid stackmind project."""
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    
    # Downgrade to 1.0.0 for migration tests
    rv_path = project / ".sync" / "RUNTIME_VERSION"
    if rv_path.exists():
        data = yaml.safe_load(rv_path.read_text())
        data["version"] = "1.0.0"
        rv_path.write_text(yaml.dump(data))
        
    return project


@pytest.fixture
def sync_path(fresh_project):
    """Return the .sync/ path of the fresh project."""
    return fresh_project / ".sync"


class TestMigrationHelpers:
    """Tests for migration helper functions."""

    def test_get_current_version(self, sync_path):
        """Should return current version from RUNTIME_VERSION."""
        version = get_current_version(sync_path)
        assert version == "1.0.0"

    def test_load_migrations(self):
        """Should load migration manifests from migrations directory."""
        migrations = load_migrations()
        assert len(migrations) >= 1
        assert all("from_version" in m for m in migrations)
        assert all("to_version" in m for m in migrations)

    def test_get_applied_migrations_empty(self, sync_path):
        """Should return empty list when no migrations applied."""
        applied = get_applied_migrations(sync_path)
        assert applied == []

    def test_save_and_get_applied_migrations(self, sync_path):
        """Should save and retrieve applied migrations."""
        save_applied_migrations(sync_path, ["1.1.0", "1.2.0"])
        applied = get_applied_migrations(sync_path)
        assert applied == ["1.1.0", "1.2.0"]

    def test_get_pending_migrations(self):
        """Should return migrations between current and target version."""
        migrations = [
            {"from_version": "1.0.0", "to_version": "1.1.0"},
            {"from_version": "1.1.0", "to_version": "1.2.0"},
            {"from_version": "1.2.0", "to_version": "2.0.0"},
        ]
        pending = get_pending_migrations("1.0.0", "1.2.0", migrations)
        assert len(pending) == 2
        assert pending[0]["to_version"] == "1.1.0"
        assert pending[1]["to_version"] == "1.2.0"


class TestMigrationActions:
    """Tests for migration action execution."""

    def test_apply_add_field(self, sync_path):
        """Should add field to YAML file."""
        migration = {
            "up": [
                {"action": "add_field", "file": "runtime/TREE.yaml", "field": "test_field", "value": "test_value"}
            ],
            "down": []
        }
        assert apply_migration(sync_path, migration)

        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert tree["test_field"] == "test_value"

    def test_apply_remove_field(self, sync_path):
        """Should remove field from YAML file."""
        # First add a field
        tree_path = sync_path / "runtime" / "TREE.yaml"
        tree = yaml.safe_load(tree_path.read_text())
        tree["to_remove"] = "value"
        tree_path.write_text(yaml.dump(tree))

        migration = {
            "up": [
                {"action": "remove_field", "file": "runtime/TREE.yaml", "field": "to_remove"}
            ],
            "down": []
        }
        assert apply_migration(sync_path, migration)

        tree = yaml.safe_load(tree_path.read_text())
        assert "to_remove" not in tree

    def test_apply_add_dir(self, sync_path):
        """Should create directory with .gitkeep."""
        migration = {
            "up": [
                {"action": "add_dir", "path": "test_dir/nested"}
            ],
            "down": []
        }
        assert apply_migration(sync_path, migration)

        dir_path = sync_path / "test_dir" / "nested"
        assert dir_path.is_dir()
        assert (dir_path / ".gitkeep").exists()

    def test_apply_remove_dir(self, sync_path):
        """Should remove directory."""
        # First create a directory
        dir_path = sync_path / "to_remove"
        dir_path.mkdir()
        (dir_path / "file.txt").write_text("test")

        migration = {
            "up": [
                {"action": "remove_dir", "path": "to_remove"}
            ],
            "down": []
        }
        assert apply_migration(sync_path, migration)
        assert not dir_path.exists()

    def test_rollback_migration(self, sync_path):
        """Should execute down actions."""
        # Add a field first
        tree_path = sync_path / "runtime" / "TREE.yaml"
        tree = yaml.safe_load(tree_path.read_text())
        tree["rollback_test"] = "value"
        tree_path.write_text(yaml.dump(tree))

        migration = {
            "up": [],
            "down": [
                {"action": "remove_field", "file": "runtime/TREE.yaml", "field": "rollback_test"}
            ]
        }
        assert rollback_migration(sync_path, migration)

        tree = yaml.safe_load(tree_path.read_text())
        assert "rollback_test" not in tree


class TestMigrateCommand:
    """Tests for the migrate command."""

    def test_migrate_check_shows_pending(self, fresh_project, sync_path, capsys):
        """--check should show pending migrations without applying."""
        result = migrate(fresh_project, check=True)
        assert result is True

    def test_migrate_up_to_date(self, fresh_project, sync_path):
        """Should report up to date when no pending migrations."""
        # Mark all migrations as applied
        migrations = load_migrations()
        applied = [m["to_version"] for m in migrations]
        save_applied_migrations(sync_path, applied)

        result = migrate(fresh_project)
        assert result is True

    def test_migrate_applies_migration(self, fresh_project, sync_path):
        """Should apply pending migrations."""
        # Ensure we have the sample migration
        migrations = load_migrations()
        if not migrations:
            pytest.skip("No migrations available")

        result = migrate(fresh_project)
        assert result is True

        # Check migration was tracked
        applied = get_applied_migrations(sync_path)
        assert len(applied) > 0

    def test_migrate_rollback(self, fresh_project, sync_path):
        """--rollback should undo last migration."""
        # First apply a migration
        migrate(fresh_project)
        applied_before = get_applied_migrations(sync_path)

        if not applied_before:
            pytest.skip("No migrations were applied")

        # Now rollback
        result = migrate(fresh_project, rollback=True)
        assert result is True

        applied_after = get_applied_migrations(sync_path)
        assert len(applied_after) == len(applied_before) - 1

    def test_migrate_no_sync_fails(self, tmp_path):
        """Should fail if no .sync/ directory."""
        result = migrate(tmp_path)
        assert result is False



class TestResolveTargets:
    """Tests for action target resolution."""

    def test_file_target(self, sync_path):
        targets = _resolve_targets(sync_path, {"file": "runtime/TREE.yaml"})
        assert len(targets) == 1
        assert targets[0].name == "TREE.yaml"

    def test_missing_file_target(self, sync_path):
        assert _resolve_targets(sync_path, {"file": "runtime/NOPE.yaml"}) == []

    def test_glob_target(self, sync_path):
        targets = _resolve_targets(sync_path, {"glob": "runtime/boot/*.yaml"})
        names = {t.name for t in targets}
        assert "claude.boot.yaml" in names
        assert len(targets) >= 5


class TestNormalizeEnumField:
    """Tests for the normalize_enum_field migration action (PLAT phase_status fix)."""

    def _set_tree_phase(self, sync_path, value):
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text())
        data["phase_status"] = value
        tree.write_text(yaml.dump(data))

    def _action(self, **over):
        action = {
            "action": "normalize_enum_field",
            "glob": "runtime/TREE.yaml",
            "field": "phase_status",
            "allowed": ["INIT", "PLANNING", "ACTIVE", "COMPLETE", "BLOCKED"],
            "mapping": {"RELEASED": "COMPLETE", "DEPLOYED": "COMPLETE"},
            "fallback": "COMPLETE",
            "preserve_to": "phase_status_detail",
        }
        action.update(over)
        return action

    def test_maps_via_mapping_and_preserves(self, sync_path):
        original = "RELEASED_DEPLOYED_HEALTHY (v1.0.5 on origin)"
        self._set_tree_phase(sync_path, original)

        assert _execute_action(sync_path, self._action(), "up")

        data = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert data["phase_status"] == "COMPLETE"
        assert data["phase_status_detail"] == original

    def test_unmapped_value_uses_fallback(self, sync_path):
        self._set_tree_phase(sync_path, "SOMETHING_WEIRD")
        assert _execute_action(sync_path, self._action(), "up")
        data = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert data["phase_status"] == "COMPLETE"
        assert data["phase_status_detail"] == "SOMETHING_WEIRD"

    def test_already_valid_value_untouched(self, sync_path):
        self._set_tree_phase(sync_path, "ACTIVE")
        assert _execute_action(sync_path, self._action(), "up")
        data = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert data["phase_status"] == "ACTIVE"
        assert "phase_status_detail" not in data

    def test_glob_applies_to_all_boot_files(self, sync_path):
        for agent in ("claude", "codex"):
            boot = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
            data = yaml.safe_load(boot.read_text())
            data["phase_status"] = "v1.0.1 RELEASED"
            boot.write_text(yaml.dump(data))

        action = self._action(glob="runtime/boot/*.yaml")
        assert _execute_action(sync_path, action, "up")

        for agent in ("claude", "codex"):
            data = yaml.safe_load(
                (sync_path / "runtime" / "boot" / f"{agent}.boot.yaml").read_text()
            )
            assert data["phase_status"] == "COMPLETE"
            assert data["phase_status_detail"] == "v1.0.1 RELEASED"

    def test_restore_field_reverses_normalization(self, sync_path):
        original = "RELEASED_DEPLOYED_HEALTHY"
        self._set_tree_phase(sync_path, original)
        _execute_action(sync_path, self._action(), "up")

        restore = {
            "action": "restore_field",
            "glob": "runtime/TREE.yaml",
            "field": "phase_status",
            "source": "phase_status_detail",
        }
        assert _execute_action(sync_path, restore, "down")

        data = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert data["phase_status"] == original
        assert "phase_status_detail" not in data


class TestPhaseStatusMigrationManifest:
    """Tests that the shipped 1.1.0 → 1.2.0 manifest loads and applies."""

    def test_manifest_loaded(self):
        migrations = load_migrations()
        targets = {m["to_version"] for m in migrations}
        assert "1.2.0" in targets

    def test_manifest_normalizes_and_is_reversible(self, sync_path):
        migration = next(m for m in load_migrations() if m["to_version"] == "1.2.0")

        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text())
        data["phase_status"] = "RELEASED_DEPLOYED_HEALTHY (v1.0.5)"
        tree.write_text(yaml.dump(data))

        assert apply_migration(sync_path, migration)
        data = yaml.safe_load(tree.read_text())
        assert data["phase_status"] == "COMPLETE"
        assert data["phase_status_detail"] == "RELEASED_DEPLOYED_HEALTHY (v1.0.5)"

        assert rollback_migration(sync_path, migration)
        data = yaml.safe_load(tree.read_text())
        assert data["phase_status"] == "RELEASED_DEPLOYED_HEALTHY (v1.0.5)"
        assert "phase_status_detail" not in data


class TestDriftMigrateValidate:
    """End-to-end: a drifted 1.0.0 runtime migrates clean (reproduces the field report)."""

    def test_drifted_runtime_migrates_and_validates(self, fresh_project, sync_path):
        # 1. Introduce the exact drift seen in the field: free-form phase_status
        #    in TREE and a boot file, plus lean INDEX orders missing the fields
        #    the strict schema used to require.
        tree = sync_path / "runtime" / "TREE.yaml"
        tdata = yaml.safe_load(tree.read_text())
        tdata["phase_status"] = "RELEASED_DEPLOYED_HEALTHY (v1.0.5 on origin; docs synced)"
        tree.write_text(yaml.dump(tdata))

        boot = sync_path / "runtime" / "boot" / "local-llm.boot.yaml"
        bdata = yaml.safe_load(boot.read_text())
        bdata["phase_status"] = "RELEASED_DEPLOYED_HEALTHY"
        boot.write_text(yaml.dump(bdata))

        index = sync_path / "work-orders" / "INDEX.yaml"
        idata = yaml.safe_load(index.read_text())
        idata["orders"] = [
            {
                "id": f"WO-{n:03d}",
                "title": f"Lean order {n}",
                "status": "COMPLETE",
                "priority": "P1",
                "dependencies": [],
            }
            for n in range(1, 4)
        ]
        index.write_text(yaml.dump(idata))

        # Sanity: before migration, validation fails on phase_status.
        before = validate(fresh_project)
        assert any("phase_status" in i.message for i in before.errors)

        # 2. Migrate (1.0.0 → 1.1.0 → 1.2.0).
        assert migrate(fresh_project) is True
        assert get_current_version(sync_path) == "3.1.0"

        # 3. Validation is now clean of the drift errors.
        after = validate(fresh_project)
        assert not any("phase_status" in i.message for i in after.errors)
        assert not any(
            "is a required property" in i.message and "orders" in i.message
            for i in after.errors
        )
        assert after.passed

        # phase_status was normalized losslessly.
        tdata = yaml.safe_load(tree.read_text())
        assert tdata["phase_status"] == "COMPLETE"
        assert "v1.0.5" in tdata["phase_status_detail"]
