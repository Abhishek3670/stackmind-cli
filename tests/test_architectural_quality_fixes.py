from __future__ import annotations
from pathlib import Path
import yaml
from validators.harness.integration_gate import IntegrationGate
from validators.kernel.daemon.authoring import (
    extract_candidate_paths,
    extract_deliverable_spec,
    derive_implementation_estimate,
    build_child_contract,
)
from validators.harness.runner import AgentRunner


def test_integration_gate_recognizes_tool_calls_audit_run_tests(tmp_path: Path):
    """Verify IntegrationGate recognizes test execution when recorded as {'tool': 'run_tests'}."""
    # Create dummy test file so integration gate detects a test suite exists
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_landing.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    gate = IntegrationGate(tmp_path)

    # Telemetry formatted exactly like Gemma's tool_calls_audit
    extra_telemetry = [
        {
            "commands_audit": [],
            "tool_calls_audit": [
                {"tool": "list_directory", "path": "."},
                {"tool": "read_file", "path": "index.html"},
                {"tool": "run_tests"},
            ],
        }
    ]

    decision = gate.evaluate(extra_telemetry=extra_telemetry)
    # The test_execution check must not report missing evidence
    test_check = next((c for c in decision.checks if c.name == "test_execution"), None)
    assert test_check is not None
    assert test_check.passed is True
    assert "Recorded 1 successful test execution(s)" in test_check.message


def test_extract_candidate_paths_recognizes_svg_and_asset_extensions():
    """Verify extract_candidate_paths recognizes svg, png, and asset files."""
    text = (
        "Task 1.1: Create directory structure (/assets, /css, /js).\n"
        "Task 1.2: Integrate provided logo files (genset_full.svg, genset_wire.svg) into assets folder.\n"
        "Task 1.3: Setup base index.html and style.css"
    )
    paths = extract_candidate_paths(text)
    assert "genset_full.svg" in paths
    assert "genset_wire.svg" in paths
    assert "index.html" in paths
    assert "style.css" in paths


def test_build_child_contract_allows_assets_and_candidate_subdirs():
    """Verify build_child_contract allows assets/**, css/**, js/** and candidate parent folders."""
    contract = build_child_contract(
        wo_id="WO-001",
        agent_id="gemini",
        role="frontend",
        deliverable_path="index.html",
        candidate_paths=["assets/genset_full.svg", "assets/genset_wire.svg", "css/style.css"],
        max_files_touched=6,
    )
    allow_modules = [rule["module"] for rule in contract["scope"]["allow"]]

    assert "assets/**" in allow_modules
    assert "css/**" in allow_modules
    assert "js/**" in allow_modules
    assert "*.svg" in allow_modules
    assert "assets/genset_full.svg" in allow_modules
    assert contract["budget"]["max_files_touched"] == 6


def test_derive_implementation_estimate_scaffolding_headroom():
    """Verify scaffolding tasks receive at least 6 files budget headroom."""
    estimate = derive_implementation_estimate(
        title="Project Scaffolding & Asset Setup",
        description="Task 1.1: Create directory structure (/assets, /css, /js). Task 1.2: Integrate genset_full.svg",
        deliverable_path="index.html",
        candidate_paths=["genset_full.svg", "genset_wire.svg"],
    )
    assert estimate is not None
    assert estimate["max_files_touched"] >= 6
    assert "index.html" in estimate["expected_files"]


def test_extract_deliverable_spec_canonicalizes_static_web_entrypoint(tmp_path: Path):
    """Verify plain web tasks route to index.html or css/style.css rather than fragmented html files."""
    # Pre-create root index.html
    (tmp_path / "index.html").write_text("<!DOCTYPE html><html></html>", encoding="utf-8")

    # Milestone 3: Landing Page Content Construction
    deliv_landing = extract_deliverable_spec(
        milestone_title="Landing Page Content Construction",
        tasks=["Implement Hero Section and Neumorphic CTA", "Implement Services Section"],
        role="frontend",
        project_path=tmp_path,
    )
    assert deliv_landing["path"] == "index.html"

    # Milestone 2: Neumorphic Design System Implementation
    deliv_style = extract_deliverable_spec(
        milestone_title="Neumorphic Design System Implementation",
        tasks=["Create CSS variables and Neumorphic card styles", "Add box-shadow helpers"],
        role="frontend",
        project_path=tmp_path,
    )
    assert deliv_style["path"] == "css/style.css"


def test_sync_index_recovers_empty_index_yaml_atomically(tmp_path: Path):
    """Verify _sync_index_and_tree_yaml recovers when INDEX.yaml is 0 bytes or corrupted."""
    sync_dir = tmp_path / ".sync"
    wos_dir = sync_dir / "work-orders"
    active_dir = wos_dir / "ACTIVE"
    completed_dir = wos_dir / "COMPLETED"
    active_dir.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy active and completed work orders
    (completed_dir / "WO-000.yaml").write_text(yaml.safe_dump({
        "id": "WO-000",
        "title": "Planning",
        "status": "COMPLETED",
        "deliverable": {"path": "PLAN.md", "type": "doc"},
    }), encoding="utf-8")

    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "title": "Scaffolding",
        "status": "ACTIVE",
        "deliverable": {"path": "index.html", "type": "code"},
    }), encoding="utf-8")

    # Corrupt or empty INDEX.yaml
    index_file = wos_dir / "INDEX.yaml"
    index_file.write_text("", encoding="utf-8")  # 0-byte file

    # Run sync
    AgentRunner._sync_index_and_tree_yaml(tmp_path)

    # INDEX.yaml must be recovered and valid
    assert index_file.is_file()
    assert index_file.stat().st_size > 0
    loaded = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert loaded["schema_version"] == 1
    assert loaded["total_active"] == 1
    assert loaded["total_completed"] == 1
    order_ids = [o["id"] for o in loaded["orders"]]
    assert "WO-000" in order_ids
    assert "WO-001" in order_ids


def test_build_child_contract_includes_frontend_scope_for_qa():
    """Verify QA (gemma) contracts allow reading/auditing frontend deliverables and assets."""
    contract = build_child_contract(
        wo_id="WO-005",
        agent_id="gemma",
        role="qa",
        deliverable_path="tests/test_qa_final_delivery.py",
    )
    allowed_modules = {rule["module"] for rule in contract["scope"]["allow"]}
    assert "tests/test_qa_final_delivery.py" in allowed_modules
    assert "tests/**" in allowed_modules
    assert "index.html" in allowed_modules
    assert "css/**" in allowed_modules
    assert "*.css" in allowed_modules
    assert "*.svg" in allowed_modules

