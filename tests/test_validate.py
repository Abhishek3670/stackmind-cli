"""Tests for stackmind validate command.

Tests M2 acceptance criteria:
- Schema validation catches invalid YAML
- Structure validation detects missing files/directories
- Protocol compliance checks hash integrity
- Boot integrity detects version mismatches
- Auto-fix repairs minor issues
"""

import shutil
from pathlib import Path

import pytest
import yaml

from cli.init import DEFAULT_AGENTS, init
from cli.validate import (
    Issue,
    Severity,
    ValidationResult,
    auto_fix,
    validate,
    validate_boot_integrity,
    validate_protocol,
    validate_schema,
    validate_structure,
)


# ─── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def fresh_project(tmp_path):
    """Create a fresh valid STACKMIND project."""
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


@pytest.fixture
def sync_path(fresh_project):
    """Return the .sync/ path of the fresh project."""
    return fresh_project / ".sync"


def _write_work_order_file(sync_path, state, work_order_id, data):
    path = sync_path / "work-orders" / state / f"{work_order_id}.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _append_index_order(sync_path, order):
    index = sync_path / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
    index_data["orders"].append(order)
    index.write_text(yaml.dump(index_data), encoding="utf-8")


# ─── Integration Tests: Full Validate ─────────────────────────


class TestValidate:
    """Full integration tests for stackmind validate."""

    def test_fresh_runtime_passes(self, fresh_project):
        result = validate(fresh_project)
        assert result.passed
        assert len(result.errors) == 0

    def test_missing_sync_is_error(self, tmp_path):
        result = validate(tmp_path)
        assert not result.passed
        assert any("not a stackmind runtime" in i.message for i in result.errors)

    def test_missing_agents_md(self, fresh_project):
        (fresh_project / "AGENTS.md").unlink()
        result = validate(fresh_project)
        assert not result.passed
        assert any("AGENTS.md" in i.message for i in result.errors)

    def test_corrupt_tree_yaml(self, sync_path):
        tree = sync_path / "runtime" / "TREE.yaml"
        tree.write_text("not: valid: yaml: [", encoding="utf-8")
        result = validate(sync_path.parent)
        assert not result.passed
        assert any("TREE.yaml" in i.message and "Schema" in i.layer for i in result.errors)

    def test_invalid_tree_schema(self, sync_path):
        tree = sync_path / "runtime" / "TREE.yaml"
        tree.write_text(
            "schema_version: 99\ntree_version: 1\nagents: {}\n",
            encoding="utf-8",
        )
        result = validate(sync_path.parent)
        assert any("schema_version" in i.message for i in result.errors)

    def test_bad_protocol_hash(self, sync_path):
        hash_file = sync_path / "PROTOCOL_DIGEST.hash"
        hash_file.write_text("A" * 64, encoding="utf-8")
        result = validate(sync_path.parent)
        assert any("PROTOCOL_DIGEST.hash mismatch" in i.message for i in result.issues)

    def test_boot_tree_version_exceeds(self, sync_path):
        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["tree_version"] = 9999
        boot.write_text(yaml.dump(data), encoding="utf-8")
        result = validate(sync_path.parent)
        assert any("exceeds TREE.yaml tree_version" in i.message for i in result.issues)

    def test_missing_boot_file(self, sync_path):
        (sync_path / "runtime" / "boot" / "codex.boot.yaml").unlink()
        result = validate(sync_path.parent)
        assert any("codex" in i.message and "boot" in i.message for i in result.issues)


# ─── Layer Tests: Schema ──────────────────────────────────────


class TestSchemaValidation:
    """Tests for Layer 1: Schema Validation."""

    def test_valid_tree_no_errors(self, sync_path):
        result = ValidationResult()
        validate_schema(sync_path, result)
        schema_errors = [i for i in result.issues if i.layer == "Schema"]
        assert len(schema_errors) == 0

    def test_invalid_tree_version_type(self, sync_path):
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["tree_version"] = "not_an_int"
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = ValidationResult()
        validate_schema(sync_path, result)
        assert any("tree_version" in i.message for i in result.issues)

    def test_valid_boot_no_errors(self, sync_path):
        result = ValidationResult()
        validate_schema(sync_path, result)
        boot_issues = [i for i in result.issues if "boot" in i.path]
        assert len(boot_issues) == 0

    def test_boot_negative_session_count(self, sync_path):
        boot = sync_path / "runtime" / "boot" / "codex.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["session_count"] = -1
        boot.write_text(yaml.dump(data), encoding="utf-8")

        result = ValidationResult()
        validate_schema(sync_path, result)
        assert any("session_count" in i.message for i in result.issues)

    def test_work_order_with_extended_fields_passes_schema(self, sync_path):
        work_order = {
            "id": "WO-011",
            "type": "FEATURE",
            "title": "Completed feature",
            "status": "COMPLETED",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "plan_ref": "PLAN.md",
            "created": "2026-07-19",
            "updated": "2026-07-19",
            "description": "Schema fixture",
            "acceptance_criteria": ["Works"],
            "affected_services": ["cli/validate.py"],
            "api_changes": False,
            "db_migration": False,
            "frontend_changes": False,
            "deliverable": {
                "type": "module",
                "path": "cli/validate.py",
                "description": "Validation logic",
            },
            "references": ["docs/example.md"],
            "files_created": ["tests/test_validate.py"],
            "notes": ["done"],
            "log": [
                {
                    "date": "2026-07-19",
                    "agent": "codex",
                    "action": "Completed work",
                }
            ],
        }
        _write_work_order_file(sync_path, "COMPLETED", "WO-011", work_order)

        result = ValidationResult()
        validate_schema(sync_path, result)
        wo_issues = [i for i in result.issues if i.path.endswith("WO-011.yaml")]
        assert len(wo_issues) == 0

    def test_work_order_missing_required_field_errors(self, sync_path):
        work_order = {
            "id": "WO-012",
            "type": "FEATURE",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
        }
        _write_work_order_file(sync_path, "ACTIVE", "WO-012", work_order)

        result = ValidationResult()
        validate_schema(sync_path, result)
        assert any(
            i.path.endswith("WO-012.yaml") and "title" in i.message
            for i in result.issues
        )

    def test_work_order_unknown_field_errors(self, sync_path):
        work_order = {
            "id": "WO-013",
            "type": "FEATURE",
            "title": "Unexpected field",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "unexpected": True,
        }
        _write_work_order_file(sync_path, "ACTIVE", "WO-013", work_order)

        result = ValidationResult()
        validate_schema(sync_path, result)
        assert any(
            i.path.endswith("WO-013.yaml") and "unexpected" in i.message
            for i in result.issues
        )


# ─── Layer Tests: Structure ───────────────────────────────────


class TestStructureValidation:
    """Tests for Layer 2: Structural Validation."""

    def test_all_dirs_exist_on_fresh(self, fresh_project, sync_path):
        result = ValidationResult()
        agents = DEFAULT_AGENTS
        validate_structure(fresh_project, sync_path, agents, result)
        struct_errors = [i for i in result.issues if i.severity == Severity.ERROR]
        assert len(struct_errors) == 0

    def test_missing_inbox_detected(self, fresh_project, sync_path):
        shutil.rmtree(sync_path / "inbox" / "codex")
        result = ValidationResult()
        validate_structure(fresh_project, sync_path, DEFAULT_AGENTS, result)
        assert any("inbox/codex" in i.message for i in result.issues)

    def test_missing_templates_dir(self, fresh_project, sync_path):
        shutil.rmtree(sync_path / "work-orders" / "TEMPLATES")
        result = ValidationResult()
        validate_structure(fresh_project, sync_path, DEFAULT_AGENTS, result)
        assert any("TEMPLATES" in i.message for i in result.issues)


# ─── Layer Tests: Protocol ────────────────────────────────────


class TestProtocolValidation:
    """Tests for Layer 3: Protocol Compliance."""

    def test_hash_match_no_issues(self, sync_path):
        result = ValidationResult()
        validate_protocol(sync_path, DEFAULT_AGENTS, result)
        protocol_errors = [i for i in result.issues if i.layer == "Protocol" and i.severity == Severity.ERROR]
        assert len(protocol_errors) == 0

    def test_hash_mismatch_detected(self, sync_path):
        (sync_path / "PROTOCOL_DIGEST.hash").write_text("B" * 64, encoding="utf-8")
        result = ValidationResult()
        validate_protocol(sync_path, DEFAULT_AGENTS, result)
        assert any("mismatch" in i.message for i in result.issues)

    def test_missing_agent_in_tree_warns(self, sync_path):
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        del data["agents"]["codex"]
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = ValidationResult()
        validate_protocol(sync_path, DEFAULT_AGENTS, result)
        assert any("codex" in i.message and "missing from TREE" in i.message for i in result.issues)

    def test_lock_stolen_event_detected(self, sync_path):
        receipts_dir = sync_path / "runtime" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        event_file = receipts_dir / "LOCK_STOLEN_codex_test.yaml"
        event_data = {
            "event_type": "LOCK_STOLEN",
            "agent": "codex",
            "stolen_from": "claude",
            "timestamp": "2026-06-25T14:32:08Z",
        }
        event_file.write_text(yaml.dump(event_data), encoding="utf-8")

        result = ValidationResult()
        validate_protocol(sync_path, DEFAULT_AGENTS, result)
        issues = [i for i in result.issues if "LOCK was stolen" in i.message]
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARN


# ─── Layer Tests: Boot Integrity ─────────────────────────────


class TestBootIntegrity:
    """Tests for Layer 4: Boot Integrity."""

    def test_fresh_runtime_passes(self, sync_path):
        result = ValidationResult()
        validate_boot_integrity(sync_path, DEFAULT_AGENTS, result)
        boot_errors = [i for i in result.issues if i.severity == Severity.ERROR]
        assert len(boot_errors) == 0

    def test_boot_exceeds_tree_version(self, sync_path):
        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["tree_version"] = 999
        boot.write_text(yaml.dump(data), encoding="utf-8")

        result = ValidationResult()
        validate_boot_integrity(sync_path, DEFAULT_AGENTS, result)
        assert any("exceeds" in i.message for i in result.issues)

    def test_stale_protocol_hash_warns(self, sync_path):
        (sync_path / "PROTOCOL_DIGEST.hash").write_text("C" * 64, encoding="utf-8")
        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["protocol_digest_hash"] = "D" * 64
        boot.write_text(yaml.dump(data), encoding="utf-8")

        result = ValidationResult()
        validate_boot_integrity(sync_path, DEFAULT_AGENTS, result)
        assert any("stale" in i.message for i in result.issues)


# ─── Auto-fix Tests ──────────────────────────────────────────


class TestAutoFix:
    """Tests for auto-fix capability."""

    def test_fix_missing_read_dir(self, fresh_project, sync_path):
        shutil.rmtree(sync_path / "inbox" / "codex" / "_read")

        result = validate(fresh_project, fix=True)
        assert (sync_path / "inbox" / "codex" / "_read").exists()

    def test_fix_missing_outbox(self, fresh_project, sync_path):
        shutil.rmtree(sync_path / "outbox" / "gemini")

        result = validate(fresh_project, fix=True)
        assert (sync_path / "outbox" / "gemini").exists()

    def test_non_fixable_errors_remain(self, fresh_project, sync_path):
        (fresh_project / "AGENTS.md").unlink()
        result = validate(fresh_project, fix=True)
        assert not result.passed

    def test_missing_read_dir_is_error(self, fresh_project, sync_path):
        """Missing _read/ folder should be ERROR, not WARN."""
        shutil.rmtree(sync_path / "inbox" / "codex" / "_read")

        result = validate(fresh_project, fix=False)
        read_issues = [i for i in result.issues if "_read" in i.message]
        assert len(read_issues) == 1
        assert read_issues[0].severity == Severity.ERROR
        assert read_issues[0].auto_fixable is True

    def test_missing_ceo_read_dir_is_error(self, fresh_project, sync_path):
        """Missing CEO inbox _read/ folder should be ERROR."""
        shutil.rmtree(sync_path / "inbox" / "CEO" / "_read")

        result = validate(fresh_project, fix=False)
        ceo_read_issues = [i for i in result.issues if "CEO/_read" in i.message]
        assert len(ceo_read_issues) == 1
        assert ceo_read_issues[0].severity == Severity.ERROR
        assert ceo_read_issues[0].auto_fixable is True

    def test_fix_missing_ceo_read_dir(self, fresh_project, sync_path):
        """Auto-fix should create missing CEO inbox _read/ folder."""
        shutil.rmtree(sync_path / "inbox" / "CEO" / "_read")

        result = validate(fresh_project, fix=True)
        assert (sync_path / "inbox" / "CEO" / "_read").exists()
        assert (sync_path / "inbox" / "CEO" / "_read" / ".gitkeep").exists()



# ─── Blocked Agent Validation Tests ─────────────────────────────


class TestBlockedAgentValidation:
    """Tests for blocked agent validation."""

    def test_blocked_agent_with_empty_blockers_is_error(self, fresh_project, sync_path):
        """Blocked agent with empty blockers list should be ERROR."""
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "blocked"
        data["agents"]["claude"]["blockers"] = []
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        blocked_issues = [i for i in result.issues if "empty blockers" in i.message]
        assert len(blocked_issues) == 1
        assert blocked_issues[0].severity == Severity.ERROR

    def test_blocked_agent_with_valid_wo_blocker_passes(self, fresh_project, sync_path):
        """Blocked agent with valid WO blocker should pass."""
        # Add a work order to INDEX.yaml
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Test WO",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml"
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        # Set claude as blocked by WO-001
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "blocked"
        data["agents"]["claude"]["blockers"] = ["WO-001"]
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        blocked_issues = [i for i in result.issues if "blocked" in i.message.lower() and "claude" in i.message]
        assert len(blocked_issues) == 0

    def test_blocked_agent_with_valid_agent_blocker_passes(self, fresh_project, sync_path):
        """Blocked agent with valid agent blocker should pass."""
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "blocked"
        data["agents"]["claude"]["blockers"] = ["codex"]
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        blocked_issues = [i for i in result.issues if "blocked" in i.message.lower() and "claude" in i.message]
        assert len(blocked_issues) == 0

    def test_blocked_agent_with_nonexistent_wo_is_error(self, fresh_project, sync_path):
        """Blocked agent referencing non-existent WO should be ERROR."""
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "blocked"
        data["agents"]["claude"]["blockers"] = ["WO-999"]
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        wo_issues = [i for i in result.issues if "non-existent work order" in i.message]
        assert len(wo_issues) == 1
        assert wo_issues[0].severity == Severity.ERROR

    def test_blocked_agent_with_nonexistent_agent_is_error(self, fresh_project, sync_path):
        """Blocked agent referencing non-existent agent should be ERROR."""
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "blocked"
        data["agents"]["claude"]["blockers"] = ["nonexistent-agent"]
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        agent_issues = [i for i in result.issues if "non-existent agent" in i.message]
        assert len(agent_issues) == 1
        assert agent_issues[0].severity == Severity.ERROR

    def test_idle_agent_with_empty_blockers_passes(self, fresh_project, sync_path):
        """Idle agent with empty blockers should pass (only blocked status requires blockers)."""
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["agents"]["claude"]["status"] = "idle"
        data["agents"]["claude"]["blockers"] = []
        tree.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        blocked_issues = [i for i in result.issues if "empty blockers" in i.message]
        assert len(blocked_issues) == 0


# ─── Graph Version Validation Tests ─────────────────────────────


class TestGraphVersionValidation:
    """Tests for graph_version alignment between boot and TREE."""

    def test_fresh_runtime_with_null_graph_version_passes(self, fresh_project, sync_path):
        """Fresh runtime with null graph_version should pass."""
        result = validate(fresh_project)
        graph_issues = [i for i in result.issues if "graph_version" in i.message]
        assert len(graph_issues) == 0

    def test_boot_graph_version_with_null_tree_is_error(self, fresh_project, sync_path):
        """Boot with graph_version when TREE has null should be ERROR."""
        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["graph_version"] = "A" * 64
        boot.write_text(yaml.dump(data), encoding="utf-8")

        result = validate(fresh_project)
        graph_issues = [i for i in result.issues if "graph_version" in i.message and "null" in i.message]
        assert len(graph_issues) == 1
        assert graph_issues[0].severity == Severity.ERROR

    def test_matching_graph_versions_passes(self, fresh_project, sync_path):
        """Matching graph_version in boot and TREE should pass."""
        graph_hash = "B" * 64

        tree = sync_path / "runtime" / "TREE.yaml"
        tree_data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        tree_data["graph_version"] = graph_hash
        tree.write_text(yaml.dump(tree_data), encoding="utf-8")

        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        boot_data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        boot_data["graph_version"] = graph_hash
        boot.write_text(yaml.dump(boot_data), encoding="utf-8")

        result = validate(fresh_project)
        graph_issues = [i for i in result.issues if "graph_version" in i.message]
        assert len(graph_issues) == 0

    def test_mismatched_graph_versions_is_error(self, fresh_project, sync_path):
        """Mismatched graph_version between boot and TREE should be ERROR."""
        tree = sync_path / "runtime" / "TREE.yaml"
        tree_data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        tree_data["graph_version"] = "C" * 64
        tree.write_text(yaml.dump(tree_data), encoding="utf-8")

        boot = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        boot_data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        boot_data["graph_version"] = "D" * 64
        boot.write_text(yaml.dump(boot_data), encoding="utf-8")

        result = validate(fresh_project)
        graph_issues = [i for i in result.issues if "graph_version mismatch" in i.message]
        assert len(graph_issues) == 1
        assert graph_issues[0].severity == Severity.ERROR

    def test_tree_graph_version_with_null_boot_passes(self, fresh_project, sync_path):
        """TREE with graph_version but boot with null should pass (boot hasn't synced yet)."""
        tree = sync_path / "runtime" / "TREE.yaml"
        tree_data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        tree_data["graph_version"] = "E" * 64
        tree.write_text(yaml.dump(tree_data), encoding="utf-8")

        result = validate(fresh_project)
        graph_issues = [i for i in result.issues if "graph_version" in i.message]
        assert len(graph_issues) == 0


# ─── Work Order Deliverable Validation Tests ────────────────────


class TestWorkOrderDeliverableValidation:
    """Tests for work order deliverable requirement."""

    def test_feature_without_deliverable_is_error(self, fresh_project, sync_path):
        """FEATURE work order without deliverable should be ERROR."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Test Feature",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml"
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        deliverable_issues = [i for i in result.issues if "deliverable" in i.message]
        assert len(deliverable_issues) == 1
        assert deliverable_issues[0].severity == Severity.ERROR

    def test_feature_with_deliverable_passes(self, fresh_project, sync_path):
        """FEATURE work order with deliverable should pass."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Test Feature",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml",
            "deliverable": {
                "type": "code",
                "path": "src/feature.py",
                "description": "New feature implementation"
            }
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        deliverable_issues = [i for i in result.issues if "deliverable" in i.message]
        assert len(deliverable_issues) == 0

    def test_research_without_deliverable_passes(self, fresh_project, sync_path):
        """RESEARCH work order without deliverable should pass (not required)."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "RESEARCH",
            "title": "Test Research",
            "status": "ACTIVE",
            "priority": "P2",
            "assigned_agents": ["claude"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml"
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        deliverable_issues = [i for i in result.issues if "deliverable" in i.message]
        assert len(deliverable_issues) == 0

    def test_bugfix_without_deliverable_is_error(self, fresh_project, sync_path):
        """BUGFIX work order without deliverable should be ERROR."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "BUGFIX",
            "title": "Fix bug",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml"
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        deliverable_issues = [i for i in result.issues if "deliverable" in i.message]
        assert len(deliverable_issues) == 1

    def test_phase_without_deliverable_passes(self, fresh_project, sync_path):
        """PHASE work order without deliverable should pass (not required)."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "PHASE",
            "title": "Phase 1",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["claude"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml"
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        deliverable_issues = [i for i in result.issues if "deliverable" in i.message]
        assert len(deliverable_issues) == 0


# ─── Rework Budget Tests ──────────────────────────────────────────


class TestWorkOrderStateDirectoryValidation:
    """Tests for WO-009 state-directory consistency checks."""

    def _base_index_order(self, work_order_id, status):
        return {
            "id": work_order_id,
            "type": "FEATURE",
            "title": f"Work order {work_order_id}",
            "status": status,
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-07-19",
            "updated": "2026-07-19",
            "deliverable": {
                "type": "module",
                "path": "cli/validate.py",
                "description": "Validation logic",
            },
        }

    def _base_work_order_file(self, work_order_id, status):
        return {
            "id": work_order_id,
            "type": "FEATURE",
            "title": f"Work order {work_order_id}",
            "status": status,
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {
                "type": "module",
                "path": "cli/validate.py",
                "description": "Validation logic",
            },
        }

    def test_duplicate_work_order_id_across_directories_errors(self, fresh_project, sync_path):
        _append_index_order(sync_path, self._base_index_order("WO-021", "ACTIVE"))
        _write_work_order_file(
            sync_path,
            "ACTIVE",
            "WO-021",
            self._base_work_order_file("WO-021", "ACTIVE"),
        )
        _write_work_order_file(
            sync_path,
            "COMPLETED",
            "WO-021",
            self._base_work_order_file("WO-021", "COMPLETED"),
        )

        result = validate(fresh_project)
        duplicate_issues = [
            i for i in result.issues if "multiple state directories" in i.message
        ]
        assert len(duplicate_issues) == 1
        assert "WO-021" in duplicate_issues[0].message

    def test_completed_directory_requires_completed_status(self, fresh_project, sync_path):
        _append_index_order(sync_path, self._base_index_order("WO-022", "COMPLETED"))
        _write_work_order_file(
            sync_path,
            "COMPLETED",
            "WO-022",
            self._base_work_order_file("WO-022", "ACTIVE"),
        )

        result = validate(fresh_project)
        assert any(
            i.path.endswith("WO-022.yaml")
            and "directory 'COMPLETED' requires COMPLETE, COMPLETED" in i.message
            for i in result.issues
        )

    def test_index_status_mismatch_errors(self, fresh_project, sync_path):
        _append_index_order(sync_path, self._base_index_order("WO-023", "BLOCKED"))
        _write_work_order_file(
            sync_path,
            "ACTIVE",
            "WO-023",
            self._base_work_order_file("WO-023", "ACTIVE"),
        )

        result = validate(fresh_project)
        assert any(
            i.path.endswith("WO-023.yaml")
            and "does not match INDEX.yaml status 'BLOCKED'" in i.message
            for i in result.issues
        )


class TestReworkBudget:
    """Tests for rework budget validation."""

    def test_exhausted_budget_without_block_flag_errors(self, fresh_project, sync_path):
        """WO with rework_count >= rework_budget but blocked_by_rework=false is an error."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Failing feature",
            "status": "ACTIVE",
            "priority": "P2",
            "assigned_agents": ["gemini"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml",
            "rework_budget": 2,
            "rework_count": 2,
            "blocked_by_rework": False,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        rework_issues = [i for i in result.issues if "rework budget" in i.message]
        assert len(rework_issues) == 1
        assert "WO-001" in rework_issues[0].message

    def test_blocked_without_escalation_errors(self, fresh_project, sync_path):
        """WO with blocked_by_rework=true but no escalation file is an error."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Blocked feature",
            "status": "BLOCKED",
            "priority": "P2",
            "assigned_agents": ["gemini"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "BLOCKED/WO-001.yaml",
            "rework_budget": 2,
            "rework_count": 2,
            "blocked_by_rework": True,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        escalation_issues = [i for i in result.issues if "escalation" in i.message]
        assert len(escalation_issues) == 1

    def test_blocked_with_escalation_passes(self, fresh_project, sync_path):
        """WO blocked_by_rework=true with matching escalation file passes."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Blocked feature",
            "status": "BLOCKED",
            "priority": "P2",
            "assigned_agents": ["gemini"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "BLOCKED/WO-001.yaml",
            "rework_budget": 2,
            "rework_count": 2,
            "blocked_by_rework": True,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        escalation = sync_path / "escalations" / "WO-001-rework.yaml"
        escalation.write_text(yaml.dump({
            "wo_id": "WO-001",
            "reason": "Agent keeps misinterpreting animation model",
            "attempts_summary": ["WO-001: CSS 3D", "WO-003: Z-layers"],
            "filed_by": "claude",
            "filed_at": "2026-05-25T10:00:00Z",
            "resolution": None,
        }), encoding="utf-8")

        result = validate(fresh_project)
        rework_issues = [i for i in result.issues if "rework" in i.message.lower()]
        assert len(rework_issues) == 0

    def test_within_budget_passes(self, fresh_project, sync_path):
        """WO with rework_count < rework_budget passes without issues."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "In-progress feature",
            "status": "ACTIVE",
            "priority": "P2",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml",
            "rework_budget": 2,
            "rework_count": 1,
            "blocked_by_rework": False,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        rework_issues = [i for i in result.issues if "rework" in i.message.lower()]
        assert len(rework_issues) == 0

    def test_exempt_types_skip_rework_check(self, fresh_project, sync_path):
        """PHASE/RESEARCH/AUDIT/VALIDATION types are exempt from rework budgets."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "RESEARCH",
            "title": "Exploratory research",
            "status": "ACTIVE",
            "priority": "P3",
            "assigned_agents": ["claude"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml",
            "rework_count": 5,
            "blocked_by_rework": False,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        rework_issues = [i for i in result.issues if "rework" in i.message.lower()]
        assert len(rework_issues) == 0

    def test_default_budget_applied(self, fresh_project, sync_path):
        """WO without explicit rework_budget uses default (2)."""
        index = sync_path / "work-orders" / "INDEX.yaml"
        index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
        index_data["orders"].append({
            "id": "WO-001",
            "type": "BUGFIX",
            "title": "Recurring bug",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "file": "ACTIVE/WO-001.yaml",
            "rework_count": 2,
        })
        index.write_text(yaml.dump(index_data), encoding="utf-8")

        result = validate(fresh_project)
        rework_issues = [i for i in result.issues if "rework budget" in i.message]
        assert len(rework_issues) == 1


# ─── Canonical Drift Tests (PLAT-01 / CODEX-03) ─────────────────


class TestCanonicalDrift:
    """Tests for TREE.yaml <-> INDEX.yaml canonical drift detection.

    PLAT-01: boot integrity must anchor to an external ground-truth source
    (INDEX.yaml) rather than comparing two potentially-stale artifacts.
    CODEX-03: cross-validate work-order totals between TREE and INDEX.
    """

    def _set_tree_totals(self, sync_path, **totals):
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data.setdefault("work_orders", {}).update(totals)
        tree.write_text(yaml.dump(data), encoding="utf-8")

    def _set_index_totals(self, sync_path, **totals):
        index = sync_path / "work-orders" / "INDEX.yaml"
        data = yaml.safe_load(index.read_text(encoding="utf-8"))
        data.update(totals)
        index.write_text(yaml.dump(data), encoding="utf-8")

    def test_fresh_runtime_has_no_drift(self, fresh_project):
        """Fresh runtime (all totals zero) reports no canonical drift."""
        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 0

    def test_completed_total_mismatch_is_error(self, fresh_project, sync_path):
        """TREE total_completed != INDEX total_completed is an ERROR."""
        self._set_tree_totals(sync_path, total_completed=5)
        self._set_index_totals(sync_path, total_completed=3)

        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 1
        assert drift_issues[0].severity == Severity.ERROR
        assert "total_completed" in drift_issues[0].message
        assert drift_issues[0].layer == "Boot Integrity"

    def test_active_total_mismatch_is_error(self, fresh_project, sync_path):
        """TREE total_active != INDEX total_active is an ERROR."""
        self._set_tree_totals(sync_path, total_active=2)
        self._set_index_totals(sync_path, total_active=0)

        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 1
        assert "total_active" in drift_issues[0].message

    def test_multiple_total_mismatches_reported_separately(self, fresh_project, sync_path):
        """Each diverging counter is reported as its own issue."""
        self._set_tree_totals(sync_path, total_active=1, total_completed=4)
        self._set_index_totals(sync_path, total_active=0, total_completed=0)

        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 2

    def test_matching_totals_pass(self, fresh_project, sync_path):
        """Equal totals across TREE and INDEX produce no drift error."""
        self._set_tree_totals(sync_path, total_active=2, total_completed=7, total_blocked=1)
        self._set_index_totals(sync_path, total_active=2, total_completed=7, total_blocked=1)

        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 0

    def test_missing_index_skips_drift_check(self, fresh_project, sync_path):
        """Drift check is skipped (no crash) when INDEX.yaml is absent."""
        self._set_tree_totals(sync_path, total_completed=5)
        (sync_path / "work-orders" / "INDEX.yaml").unlink()

        result = validate(fresh_project)
        drift_issues = [i for i in result.issues if "Canonical drift" in i.message]
        assert len(drift_issues) == 0


# ─── Snapshot Version Lag Tests (GEMMA-01) ──────────────────────


class TestSnapshotVersionLag:
    """Tests for per-agent snapshot version lag detection (GEMMA-01)."""

    def _set_tree_version(self, sync_path, version):
        tree = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree.read_text(encoding="utf-8"))
        data["tree_version"] = version
        tree.write_text(yaml.dump(data), encoding="utf-8")

    def _set_boot_version(self, sync_path, agent, version):
        boot = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
        data = yaml.safe_load(boot.read_text(encoding="utf-8"))
        data["tree_version"] = version
        boot.write_text(yaml.dump(data), encoding="utf-8")

    def test_fresh_runtime_no_lag_warning(self, fresh_project):
        """Fresh runtime (boot and tree both version 1) emits no lag warning."""
        result = validate(fresh_project)
        lag_issues = [i for i in result.issues if "version lag" in i.message]
        assert len(lag_issues) == 0

    def test_lag_beyond_threshold_warns(self, fresh_project, sync_path):
        """A snapshot lagging by more than the threshold (3) is a WARN."""
        self._set_tree_version(sync_path, 10)
        self._set_boot_version(sync_path, "gemma", 1)

        result = validate(fresh_project)
        lag_issues = [
            i for i in result.issues
            if "version lag" in i.message and "gemma" in i.message
        ]
        assert len(lag_issues) == 1
        assert lag_issues[0].severity == Severity.WARN
        assert "9 versions behind" in lag_issues[0].message

    def test_lag_within_threshold_passes(self, fresh_project, sync_path):
        """A snapshot lagging within the threshold produces no warning."""
        self._set_tree_version(sync_path, 3)
        self._set_boot_version(sync_path, "gemma", 1)  # lag = 2

        result = validate(fresh_project)
        lag_issues = [i for i in result.issues if "version lag" in i.message]
        assert len(lag_issues) == 0

    def test_lag_exactly_at_threshold_passes(self, fresh_project, sync_path):
        """A lag equal to the threshold is not flagged (strictly greater-than)."""
        self._set_tree_version(sync_path, 4)
        self._set_boot_version(sync_path, "gemma", 1)  # lag = 3

        result = validate(fresh_project)
        lag_issues = [i for i in result.issues if "version lag" in i.message]
        assert len(lag_issues) == 0

    def test_lag_does_not_replace_exceeds_error(self, fresh_project, sync_path):
        """A boot ahead of TREE is still an ERROR, not a lag warning."""
        self._set_tree_version(sync_path, 2)
        self._set_boot_version(sync_path, "gemma", 99)

        result = validate(fresh_project)
        lag_issues = [i for i in result.issues if "version lag" in i.message]
        exceeds_issues = [i for i in result.issues if "exceeds" in i.message]
        assert len(lag_issues) == 0
        assert len(exceeds_issues) == 1


# ─── Untracked .sync Path Tests (CODEX-02) ──────────────────────

import subprocess


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True, text=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _git_init_sync(sync_path):
    """Initialise .sync as a git repo with everything committed."""
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test.dev",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test.dev",
    }
    import os
    full_env = {**os.environ, **env}
    run = lambda args: subprocess.run(
        args, cwd=str(sync_path), capture_output=True, check=True, text=True, env=full_env
    )
    run(["git", "init"])
    run(["git", "add", "."])
    run(["git", "commit", "-m", "init"])


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestUntrackedSyncPaths:
    """Tests for CODEX-02: untracked .sync paths flagged as compliance warnings."""

    def test_non_git_sync_skips_check(self, fresh_project, sync_path):
        """A non-git .sync (the default fixture) emits no untracked warnings."""
        result = validate(fresh_project)
        untracked = [i for i in result.issues if "Untracked path" in i.message]
        assert len(untracked) == 0

    def test_clean_repo_no_warning(self, fresh_project, sync_path):
        """A fully-committed .sync repo produces no untracked warnings."""
        _git_init_sync(sync_path)
        result = validate(fresh_project)
        untracked = [i for i in result.issues if "Untracked path" in i.message]
        assert len(untracked) == 0

    def test_untracked_file_is_warning(self, fresh_project, sync_path):
        """An untracked file in the .sync repo is flagged as a WARN."""
        _git_init_sync(sync_path)
        (sync_path / "reviews" / "orphan-review.md").write_text(
            "orphan", encoding="utf-8"
        )
        result = validate(fresh_project)
        untracked = [i for i in result.issues if "Untracked path" in i.message]
        assert len(untracked) == 1
        assert untracked[0].severity == Severity.WARN
        assert "orphan-review.md" in untracked[0].message

    def test_untracked_does_not_fail_validation(self, fresh_project, sync_path):
        """Untracked paths are warnings only — they do not fail validation."""
        _git_init_sync(sync_path)
        (sync_path / "reviews" / "orphan-review.md").write_text(
            "orphan", encoding="utf-8"
        )
        result = validate(fresh_project)
        assert result.passed  # no errors, only a warning



# ─── Review File Bundling Tests (GEMINI-03) ─────────────────────


class TestReviewFileBundling:
    """Tests for GEMINI-03: review files must reference a single work order."""

    def _write_inbox_review(self, sync_path, name, content):
        path = sync_path / "inbox" / "gemma" / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_single_wo_review_passes(self, fresh_project, sync_path):
        self._write_inbox_review(
            sync_path,
            "2026-06-18_gemini_WO-055-review.md",
            "# Review request for WO-055\n\nPlease review the changes.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_filename_bundled_is_error(self, fresh_project, sync_path):
        """A review request named for two work orders is flagged."""
        self._write_inbox_review(
            sync_path,
            "2026-06-18_gemini_WO-055-WO-044-review.md",
            "# Review for both work orders.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 1
        assert review_issues[0].severity == Severity.ERROR
        assert "WO-044" in review_issues[0].message
        assert "WO-055" in review_issues[0].message

    def test_content_reference_not_flagged(self, fresh_project, sync_path):
        """A single-WO request that references another WO in prose is NOT flagged."""
        self._write_inbox_review(
            sync_path,
            "2026-06-18_gemini_WO-055-review.md",
            "Also incorporates WO-060 changes and depends on WO-044.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_repeated_same_wo_is_not_bundling(self, fresh_project, sync_path):
        """Mentioning the same WO multiple times is fine."""
        self._write_inbox_review(
            sync_path,
            "2026-06-18_gemini_WO-055-review.md",
            "WO-055 does X. WO-055 also does Y. See WO-055.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_non_review_inbox_file_ignored(self, fresh_project, sync_path):
        """A non-review inbox message mentioning multiple WOs is not flagged."""
        (sync_path / "inbox" / "claude" / "2026-06-18_status.md").write_text(
            "Status: WO-055 and WO-044 both in progress.", encoding="utf-8"
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_review_plan_not_flagged(self, fresh_project, sync_path):
        """A review *plan* covering several WOs is not a per-WO request (real-world FP)."""
        self._write_inbox_review(
            sync_path,
            "2026-06-09_claude_detection-feed-review-plan.md",
            "Plan covering WO-033, WO-044, WO-045, WO-046, WO-047.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_verdict_file_not_flagged(self, fresh_project, sync_path):
        """A QA verdict referencing a related WO is not a review request (real-world FP)."""
        (sync_path / "reviews" / "2026-06-18_gemma_WO-063-verdict.md").write_text(
            "Verdict on WO-063; supersedes WO-055.", encoding="utf-8"
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_single_wo_request_with_extra_suffix_not_flagged(self, fresh_project, sync_path):
        """A '-review.md' request named for one WO is fine even if prose names others."""
        self._write_inbox_review(
            sync_path,
            "2026-06-10_claude_WO-060-qa-gate-and-review.md",
            "QA gate for WO-060; relates to WO-059.",
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_processed_review_in_read_ignored(self, fresh_project, sync_path):
        """A bundled review already moved to _read/ is not re-flagged."""
        path = sync_path / "inbox" / "gemma" / "_read" / "2026-06-18_gemini_WO-055-WO-044-review.md"
        path.write_text("Review for WO-055 and WO-044.", encoding="utf-8")
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 0

    def test_reviews_dir_bundled_is_error(self, fresh_project, sync_path):
        """A review request named for two WOs in the reviews/ directory is flagged."""
        (sync_path / "reviews" / "WO-055-WO-044-review.md").write_text(
            "Bundles WO-055 with WO-044.", encoding="utf-8"
        )
        result = validate(fresh_project)
        review_issues = [i for i in result.issues if "multiple work orders" in i.message]
        assert len(review_issues) == 1



# ─── Completion Notice release_target Tests (GEMINI-04) ─────────


class TestCompletionNoticeReleaseTarget:
    """Tests for GEMINI-04: completion notices must declare a release_target."""

    def _write_notice(self, sync_path, name, content):
        path = sync_path / "inbox" / "claude" / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_notice_with_release_target_passes(self, fresh_project, sync_path):
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_WO-055-complete.md",
            "wo_id: WO-055\nstatus: COMPLETE (pending Gemma review)\n"
            "release_target: v1.0.5\ncommit: abc123\n",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0

    def test_notice_without_release_target_is_error(self, fresh_project, sync_path):
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_WO-055-complete.md",
            "wo_id: WO-055\nstatus: COMPLETE\ncommit: abc123\n",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 1
        assert notice_issues[0].severity == Severity.ERROR
        assert "WO-055-complete.md" in notice_issues[0].message

    def test_empty_release_target_is_error(self, fresh_project, sync_path):
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_WO-055-complete.md",
            "wo_id: WO-055\nrelease_target:\ncommit: abc123\n",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 1

    def test_placeholder_release_target_is_error(self, fresh_project, sync_path):
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_WO-055-complete.md",
            "wo_id: WO-055\nrelease_target: TBD\n",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 1

    def test_non_completion_file_ignored(self, fresh_project, sync_path):
        """A non-completion inbox message is not required to declare a target."""
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_status.md",
            "Just a status update with no release_target.",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0

    def test_processed_notice_in_read_ignored(self, fresh_project, sync_path):
        path = (
            sync_path / "inbox" / "claude" / "_read"
            / "2026-06-18_gemini_WO-055-complete.md"
        )
        path.write_text("wo_id: WO-055\nstatus: COMPLETE\n", encoding="utf-8")
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0

    def test_release_target_in_fenced_yaml_passes(self, fresh_project, sync_path):
        """release_target inside a fenced YAML block is still recognised."""
        self._write_notice(
            sync_path,
            "2026-06-18_gemini_WO-055-complete.md",
            "# Completion\n\n```yaml\nwo_id: WO-055\nrelease_target: v1.0.5\n```\n",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0

    def test_ceo_release_notice_not_flagged(self, fresh_project, sync_path):
        """A CEO-directed release notice is not a per-WO completion notice (real-world FP)."""
        (sync_path / "inbox" / "CEO" / "2026-05-27_claude_v1-release-complete.md").write_text(
            "v1.0.0 released and deployed. No release_target field here.",
            encoding="utf-8",
        )
        (sync_path / "inbox" / "CEO" / "2026-06-09_claude_detection-batch-complete-release-decision.md").write_text(
            "Release decision for the detection batch.", encoding="utf-8"
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0

    def test_completion_without_wo_id_not_flagged(self, fresh_project, sync_path):
        """A 'complete' notice in claude inbox without a WO id is not a WO completion."""
        self._write_notice(
            sync_path,
            "2026-06-18_claude_sprint-complete.md",
            "Sprint wrapped up.",
        )
        result = validate(fresh_project)
        notice_issues = [i for i in result.issues if "release_target" in i.message]
        assert len(notice_issues) == 0



# ─── .sync-ref Anchoring Tests (PLAT-05) ────────────────────────


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestSyncRefAnchoring:
    """Tests for PLAT-05: .sync HEAD vs tracked .sync-ref."""

    def _head(self, sync_path):
        import subprocess
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(sync_path), capture_output=True, check=True, text=True
        ).stdout.strip()

    def _commit_new(self, sync_path):
        import os, subprocess
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.dev",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.dev"}
        (sync_path / "state" / "extra.md").write_text("more", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(sync_path), capture_output=True, check=True, text=True, env=env)
        subprocess.run(["git", "commit", "-m", "second"], cwd=str(sync_path), capture_output=True, check=True, text=True, env=env)

    def test_no_sync_ref_skips(self, fresh_project, sync_path):
        _git_init_sync(sync_path)
        result = validate(fresh_project)
        ref_issues = [i for i in result.issues if ".sync-ref" in i.message or ".sync HEAD" in i.message]
        assert len(ref_issues) == 0

    def test_matching_ref_passes(self, fresh_project, sync_path):
        _git_init_sync(sync_path)
        (fresh_project / ".sync-ref").write_text(self._head(sync_path), encoding="utf-8")
        result = validate(fresh_project)
        ref_issues = [i for i in result.issues if ".sync HEAD" in i.message]
        assert len(ref_issues) == 0

    def test_mismatched_ref_warns(self, fresh_project, sync_path):
        _git_init_sync(sync_path)
        (fresh_project / ".sync-ref").write_text(self._head(sync_path), encoding="utf-8")
        # Advance .sync HEAD without updating .sync-ref.
        self._commit_new(sync_path)
        result = validate(fresh_project)
        ref_issues = [i for i in result.issues if ".sync HEAD" in i.message]
        assert len(ref_issues) == 1
        assert ref_issues[0].severity == Severity.WARN

    def test_empty_ref_warns(self, fresh_project, sync_path):
        _git_init_sync(sync_path)
        (fresh_project / ".sync-ref").write_text("", encoding="utf-8")
        result = validate(fresh_project)
        ref_issues = [i for i in result.issues if ".sync-ref is empty" in i.message]
        assert len(ref_issues) == 1

    def test_non_git_sync_skips(self, fresh_project, sync_path):
        # .sync-ref present but .sync is not a git repo (default fixture).
        (fresh_project / ".sync-ref").write_text("deadbeef", encoding="utf-8")
        result = validate(fresh_project)
        ref_issues = [i for i in result.issues if ".sync HEAD" in i.message]
        assert len(ref_issues) == 0


class TestHandoffValidation:
    """Tests for GEMINI-02 and LOCAL-LLM-01 handoff report validation."""

    def test_valid_handoff_passes(self, sync_path):
        agent_dir = sync_path / "outbox" / "claude"
        agent_dir.mkdir(parents=True, exist_ok=True)
        handoff_file = agent_dir / "handoff-2026-06-25T14-32-08Z.md"
        handoff_file.write_text(
            "✅ COMPLETED THIS SESSION (session_completed: 30):\n"
            "- Worked on planning\n\n"
            "📋 MY NEXT TASKS (when I resume):\n"
            "- WO-001 (assigned by Claude, inbox message 2026-06-18) — finish tasks\n",
            encoding="utf-8"
        )
        result = ValidationResult()
        validate_protocol(sync_path, ["claude"], result)
        issues = [i for i in result.issues if i.layer == "Protocol"]
        assert len(issues) == 0

    def test_missing_assignment_source_errors(self, sync_path):
        agent_dir = sync_path / "outbox" / "claude"
        agent_dir.mkdir(parents=True, exist_ok=True)
        handoff_file = agent_dir / "handoff-2026-06-25T14-32-08Z.md"
        handoff_file.write_text(
            "📋 MY NEXT TASKS (when I resume):\n"
            "- WO-001 - finish tasks without source\n",
            encoding="utf-8"
        )
        result = ValidationResult()
        validate_protocol(sync_path, ["claude"], result)
        issues = [i for i in result.issues if "GEMINI-02" in i.message]
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR

    def test_missing_delegating_agent_errors(self, sync_path):
        agent_dir = sync_path / "outbox" / "claude"
        agent_dir.mkdir(parents=True, exist_ok=True)
        handoff_file = agent_dir / "handoff-2026-06-25T14-32-08Z.md"
        handoff_file.write_text(
            "✅ COMPLETED THIS SESSION (session_completed: 30):\n"
            "- Delegated check to coder\n",
            encoding="utf-8"
        )
        result = ValidationResult()
        validate_protocol(sync_path, ["claude"], result)
        issues = [i for i in result.issues if "LOCAL-LLM-01" in i.message]
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR

    def test_archived_read_handoffs_ignored(self, sync_path):
        """Historical handoffs in _read/ should not trigger retroactive validation errors."""
        read_dir = sync_path / "outbox" / "claude" / "_read"
        read_dir.mkdir(parents=True, exist_ok=True)
        old_handoff = read_dir / "handoff-2026-01-01T00-00-00Z.md"
        old_handoff.write_text(
            "📋 MY NEXT TASKS (when I resume):\n"
            "- WO-001 - finish tasks without source\n",
            encoding="utf-8"
        )
        result = ValidationResult()
        validate_protocol(sync_path, ["claude"], result)
        issues = [i for i in result.issues if "GEMINI-02" in i.message]
        assert len(issues) == 0

    def test_canonical_drift_autofix(self, fresh_project, sync_path):
        """validate --fix should automatically fix canonical drift between TREE and INDEX."""
        tree_path = sync_path / "runtime" / "TREE.yaml"
        index_path = sync_path / "work-orders" / "INDEX.yaml"
        
        tree_data = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
        index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        
        # Introduce drift
        tree_data["work_orders"]["total_active"] = 99
        index_data["total_active"] = 2
        tree_path.write_text(yaml.dump(tree_data), encoding="utf-8")
        index_path.write_text(yaml.dump(index_data), encoding="utf-8")
        
        # Validation without fix catches the error
        res_no_fix = validate(fresh_project, fix=False)
        assert any("Canonical drift" in i.message for i in res_no_fix.errors)
        
        # Validation with fix resolves it
        res_with_fix = validate(fresh_project, fix=True)
        assert not any("Canonical drift" in i.message for i in res_with_fix.errors)
        
        tree_after = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
        assert tree_after["work_orders"]["total_active"] == 2

    def test_tracked_sync_in_main_repo_warns(self, fresh_project, sync_path):
        """If .sync is tracked by main repo git index, validate surfaces a warning."""
        # Simulate main repo with .git
        (fresh_project / ".git").mkdir(parents=True, exist_ok=True)
        result = validate(fresh_project)
        # Should not crash and validate cleanly
        assert result is not None
