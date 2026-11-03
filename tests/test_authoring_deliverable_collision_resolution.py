import yaml
from pathlib import Path
from validators.harness.authoring_yaml import normalize_work_order_data
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.authoring_readiness import validate_authoring_readiness


def test_work_order_with_revision_note_absorbs_into_amendments():
    """Verify that conversational revision_note and unexpected keys are cleanly absorbed and stripped."""
    raw = {
        "id": "WO-005",
        "type": "FEATURE",
        "title": "QA DOM contract check",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "revision": 2,
        "deliverable": {
            "type": "code",
            "path": "tests/test_dom.py",
            "description": "DOM check",
        },
        "revision_note": "Amended WO-005 contract to revision 2: corrected agent_id",
        "notes": "Single note string",
        "acceptance_criteria": "Must pass contract verification",
        "extra_conversational_key": "ignore me",
    }
    normalized = normalize_work_order_data(raw)
    assert "revision_note" not in normalized
    assert "extra_conversational_key" not in normalized
    assert normalized["revision"] == 2
    assert normalized["amendments"] == [
        {"revision": 2, "reason": "Amended WO-005 contract to revision 2: corrected agent_id", "agent": "architect"}
    ]
    assert normalized["notes"] == ["Single note string"]
    assert normalized["acceptance_criteria"] == ["Must pass contract verification"]

    # Must pass AuthoringGate validation without schema error
    gate = AuthoringGate()
    dec = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-005.yaml",
        yaml.safe_dump(normalized, sort_keys=False),
        agent="claude",
    )
    assert dec.passed, dec.errors


def test_compiler_resolves_qa_and_frontend_deliverable_collision(tmp_path: Path):
    """Verify that authoring_compiler resolves overlapping deliverable ownership between QA and Frontend."""
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # WO-001: Frontend implements index.html
    wo1 = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Implement Scientific Calculator UI",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "milestone_id": "Milestone 1",
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "Calculator UI",
        },
        "deliverables": [
            {"type": "code", "path": "index.html", "description": "Calculator UI"},
            {"type": "code", "path": "assets/css/style.css", "description": "Styles"},
        ],
        "test_plan": [
            {"sources": ["index.html"], "tests": ["tests/test_calculator.py"]}
        ],
    }
    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1, sort_keys=False), encoding="utf-8")

    # WO-005: QA DOM contract verification created to inspect index.html, naively claiming index.html
    wo5 = {
        "id": "WO-005",
        "type": "VALIDATION",
        "title": "QA: Perform DOM contract verification of index.html",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": [],  # Initially parallel!
        "milestone_id": "Milestone 1",
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "DOM contract verification of index.html",
        },
        "deliverables": [
            {"type": "code", "path": "index.html", "description": "DOM contract check"},
        ],
        "test_plan": [
            {"sources": ["index.html"], "tests": ["tests/test_calculator.py"]}
        ],
    }
    (active_dir / "WO-005.yaml").write_text(yaml.safe_dump(wo5, sort_keys=False), encoding="utf-8")

    # Run compiler
    res = compile_authoring_artifacts(tmp_path)

    # Check updated WO-005 on disk
    wo5_updated = yaml.safe_load((active_dir / "WO-005.yaml").read_text(encoding="utf-8"))
    assert "WO-001" in wo5_updated["dependencies"]
    # Verify index.html was pruned from secondary deliverables and primary deliverable was diverted
    if "deliverables" in wo5_updated:
        assert not any(d.get("path") == "index.html" for d in (wo5_updated["deliverables"] or []))

    # Evaluate authoring readiness gate
    readiness = validate_authoring_readiness(tmp_path)
    ownership_issues = [i for i in readiness.issues if i.code == "OVERLAPPING_FILE_OWNERSHIP"]
    assert not ownership_issues, f"OVERLAPPING_FILE_OWNERSHIP still triggered: {ownership_issues}"


def test_compiler_resolves_parallel_implementation_deliverable_collision(tmp_path: Path):
    """Verify that authoring_compiler orders parallel implementation work orders sharing a deliverable."""
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo1 = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Base Calculator HTML",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "milestone_id": "Milestone 1",
        "deliverable": {"type": "code", "path": "index.html", "description": "Base HTML"},
        "test_plan": [{"sources": ["index.html"], "tests": ["tests/test_calc.py"]}],
    }
    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1, sort_keys=False), encoding="utf-8")

    wo2 = {
        "id": "WO-002",
        "type": "FEATURE",
        "title": "Enhance Calculator HTML with Keyboard Support",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemini"],
        "dependencies": [],  # Parallel!
        "milestone_id": "Milestone 1",
        "deliverable": {"type": "code", "path": "index.html", "description": "Enhanced HTML"},
        "test_plan": [{"sources": ["index.html"], "tests": ["tests/test_calc.py"]}],
    }
    (active_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2, sort_keys=False), encoding="utf-8")

    res = compile_authoring_artifacts(tmp_path)

    wo2_updated = yaml.safe_load((active_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert "WO-001" in wo2_updated["dependencies"]

    readiness = validate_authoring_readiness(tmp_path)
    ownership_issues = [i for i in readiness.issues if i.code == "OVERLAPPING_FILE_OWNERSHIP"]
    assert not ownership_issues, f"OVERLAPPING_FILE_OWNERSHIP triggered: {ownership_issues}"


def test_architect_amends_active_work_order_with_amendments_auto_bumps_and_avoids_conflict(tmp_path: Path):
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    wo = {
        "id": "WO-007",
        "type": "FEATURE",
        "title": "Implement responsive dark-mode styling",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "revision": 2,
        "deliverable": {"type": "code", "path": "styles.css", "description": "Styles"},
        "description": "Styles for calculator",
        "acceptance_criteria": ["Valid css"],
    }
    (active_dir / "WO-007.yaml").write_text(yaml.safe_dump(wo, sort_keys=False), encoding="utf-8")

    gate = AuthoringGate()
    amended = dict(wo)
    amended["description"] += " updated with new details"
    amended["revision"] = 2  # Same revision as on disk!
    amended["amendments"] = [{"revision": 2, "reason": "Recovery repair (architect_fix) for WO-007"}]

    dec = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-007.yaml",
        yaml.safe_dump(amended, sort_keys=False),
        agent="claude",
        project_root=tmp_path,
    )
    assert dec.passed, dec.errors
    reloaded = yaml.safe_load(dec.normalized_content)
    assert reloaded["revision"] == 3
    assert reloaded["amendments"][-1]["revision"] == 3

    # Without amendments or higher revision, an arbitrary overwrite by fresh gate is rejected
    fresh_gate = AuthoringGate()
    unauthorized_amended = dict(wo)
    unauthorized_amended["title"] = "Ungoverned overwrite"
    dec_fail = fresh_gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-007.yaml",
        yaml.safe_dump(unauthorized_amended, sort_keys=False),
        agent="claude",
        project_root=tmp_path,
    )
    assert not dec_fail.passed
    assert any("Overwrite conflict" in e for e in dec_fail.errors)


def test_authoring_gate_sanitizes_amendments_and_log_schema_variations(tmp_path: Path):
    """Verify that amendments with author/timestamp and log with timestamp/entry are sanitized to schema."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    raw_wo = {
        "id": "WO-012",
        "type": "BUGFIX",
        "title": "Backend recovery repair",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "revision": 2,
        "deliverable": {"type": "code", "path": "calculator-core.js", "description": "Core"},
        "description": "Fix core calculation logic",
        "acceptance_criteria": ["All tests pass"],
        "amendments": [
            {
                "author": "claude",
                "timestamp": "2026-11-04T00:19:00Z",
                "entry": "Amended contract to broaden scope",
            }
        ],
        "log": [
            {
                "timestamp": "2026-11-04T00:02:40.111086+05:30",
                "entry": "Re-authored styles.css satisfying acceptance criteria",
            }
        ],
    }
    gate = AuthoringGate(project_root=tmp_path)
    dec = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-012.yaml",
        yaml.safe_dump(raw_wo, sort_keys=False),
        agent="claude",
        project_root=tmp_path,
    )
    assert dec.passed, dec.errors
    normalized = yaml.safe_load(dec.normalized_content)
    # amendments sanitized
    assert "author" not in normalized["amendments"][0]
    assert normalized["amendments"][0]["agent"] == "claude"
    assert normalized["amendments"][0]["date"] == "2026-11-04T00:19:00Z"
    assert normalized["amendments"][0]["reason"] == "Amended contract to broaden scope"
    assert normalized["amendments"][0]["revision"] == 2
    # log sanitized
    assert "entry" not in normalized["log"][0]
    assert normalized["log"][0]["action"] == "Re-authored styles.css satisfying acceptance criteria"


def test_authoring_gate_repairs_unquoted_colon_in_description(tmp_path: Path):
    """Verify that descriptions containing colons (EXTRACT PURE LOGIC: ...) parse and repair cleanly."""
    raw_yaml = """id: WO-003
type: FEATURE
title: Build calculator core
status: ACTIVE
priority: P1
assigned_agents:
  - codex
dependencies: []
deliverable:
  type: code
  path: calculator-core.js
  description: Core logic
acceptance_criteria:
  - Functions are exposed
description: Build calculator behavior. EXTRACT PURE LOGIC: expose a global CalculatorCore
"""
    gate = AuthoringGate(project_root=tmp_path)
    dec = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-003.yaml",
        raw_yaml,
        agent="claude",
        project_root=tmp_path,
    )
    assert dec.passed, dec.errors
    parsed = yaml.safe_load(dec.normalized_content)
    assert "EXTRACT PURE LOGIC: expose a global CalculatorCore" in parsed["description"]


def test_ephemeral_test_files_ignored_by_contract_gate():
    """Verify that smoke logs and test logs do not trip contract max_files_touched."""
    from validators.harness.contract_gate import _is_ephemeral_test_file
    assert _is_ephemeral_test_file("tests/_wo012_smoke_log.txt") is True
    assert _is_ephemeral_test_file("tests/_smoke_check.log") is True
    assert _is_ephemeral_test_file("temp.tmp") is True
    assert _is_ephemeral_test_file("src/calculator.js") is False
    assert _is_ephemeral_test_file("tests/test_calculator.py") is False


def test_plan_structure_auto_repair_in_verification():
    """Verify that plan structure without checklist items is repaired into valid milestones."""
    from validators.harness.plan import validate_plan_structure, repair_plan_structure
    bad_plan = """# Project Plan

## Architecture Overview
System architecture details.

## Milestones
Milestone 1: Scaffold index.html
Milestone 2: Implement styling
"""
    valid, err = validate_plan_structure(bad_plan)
    assert not valid
    assert any("must contain at least one checklist item" in e for e in err)

    repaired = repair_plan_structure(bad_plan)
    valid_repaired, _ = validate_plan_structure(repaired)
    assert valid_repaired
    assert "- [ ]" in repaired
