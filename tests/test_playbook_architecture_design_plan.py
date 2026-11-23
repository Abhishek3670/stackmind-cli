"""Comprehensive tests for mandatory Playbook artifacts in .sync/playbook/
(ARCHITECTURE.md, DESIGN.md, PLAN.md), initialization behavior, architect permissions,
tooling, and worker contract grounding.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from cli.init import init
from validators.harness.plan import (
    resolve_playbook_file,
    validate_architecture_structure,
    validate_design_structure,
    validate_plan_structure,
    validate_playbook_artifacts,
    PLAN_GENERATION_INSTRUCTIONS,
    ARCHITECTURE_RESEARCH_INSTRUCTIONS,
    ARCHITECTURE_SPEC_TEMPLATE,
    DESIGN_SPEC_TEMPLATE,
)
from validators.kernel.identity import is_architect_writable_path
from validators.kernel.providers.gateway import PLANNING_CORE_TOOL_NAMES
from validators.harness.task_contract import compile_worker_task_contract, render_worker_system


SAMPLE_VALID_ARCHITECTURE = """# Architecture Specification: web-calculator

## System Architecture
The application is a lightweight, responsive client-side web calculator running in the browser.
It separates presentation from arithmetic evaluation logic using strict event-driven contracts.

## Tech Stack & Dependencies
- Core Language: Vanilla JavaScript (ES6+)
- Styling: Pure CSS3 with CSS Custom Properties
- Markup: Semantic HTML5
- Persistence / DB: None (Stateless browser runtime)
- Dependencies: None

## Directory & Module Structure
- `index.html`: Scaffolding markup, layout containers, and DOM hook elements
- `assets/css/style.css`: Responsive layout grid, color variables, and button states
- `assets/js/calculator.js`: Arithmetic engine and UI event binding
- `tests/test_calculator.py`: Verification assertions

## Data & Interface Contracts
- Container element: `#display`
- Digit buttons: `.btn-digit` with `data-val`
- Operator buttons: `.btn-op` with `data-val`
- Clear button: `#clear`
"""

SAMPLE_VALID_DESIGN = """# Design Specification: web-calculator

## UI/UX Design & Theme
- Visual Tone: Modern Neomorphic Dark Theme
- Color Palette:
  --bg-color: #1e1e24
  --calc-bg: #2b2b36
  --display-bg: #141419
  --display-text: #00ffc8
  --btn-digit-bg: #383848
  --btn-op-bg: #ff6b6b
  --btn-equal-bg: #4ecdc4
- Typography: Monospace digital display font and clean sans-serif keys

## Component Hierarchy & Layout
- Layout Grid: 4-column CSS grid with 12px gap
- Display Screen: #display with right-aligned overflow scrolling
- Keypad Grid: .button-grid containing .btn-digit and .btn-op elements

## Interaction & States
- User Interaction: Real-time keypad clicking and keyboard input binding
- Visual States: Active button depressions (:active transform: scale(0.96)), hover elevation
- Accessibility: aria-label on all operator and clear keys, high contrast display
"""

SAMPLE_VALID_PLAN = """# Project Plan: web-calculator

This document serves as the living architectural roadmap for web-calculator.

## Current Architecture
- Client-side vanilla web calculator.
- Stack & Dependencies: Pure HTML5, CSS3, ES6 JS.

## Milestones & Roadmap
- [ ] Milestone 1: Scaffolding (Agent: codex)
  - [ ] Task 1.1: Create HTML structure
- [ ] Milestone 2: UI Styling (Agent: gemini)
  - [ ] Task 2.1: Implement CSS theme
- [ ] Milestone 3: Verification (Agent: gemma)
  - [ ] Task 3.1: Verify calculator behaviors
"""


def test_init_scaffolds_empty_playbook_and_omits_root_plan(tmp_path: Path):
    """stackmind init must create .sync/playbook/ with .gitkeep and NOT create root PLAN.md."""
    project_path = tmp_path / "new_project"
    init(project_path, name="PlaybookTestProject", no_git=True)

    playbook_dir = project_path / ".sync" / "playbook"
    assert playbook_dir.is_dir(), ".sync/playbook/ directory must be created during init"
    assert (playbook_dir / ".gitkeep").is_file(), ".sync/playbook/.gitkeep must exist"

    # Root PLAN.md must NOT exist in fresh projects
    root_plan = project_path / "PLAN.md"
    assert not root_plan.exists(), "PLAN.md should not be created at workspace root during stackmind init"


def test_resolve_playbook_file_precedence(tmp_path: Path):
    """resolve_playbook_file resolves .sync/playbook/ first, with fallback to root or .sync/."""
    ws = tmp_path / "ws"
    ws.mkdir()
    playbook_dir = ws / ".sync" / "playbook"
    playbook_dir.mkdir(parents=True)

    # 1. Defaults to .sync/playbook/ when no files exist
    resolved = resolve_playbook_file(ws, "PLAN.md")
    assert resolved == playbook_dir / "PLAN.md"

    # 2. Falls back to root PLAN.md when only root exists
    root_plan = ws / "PLAN.md"
    root_plan.write_text("# Root Plan", encoding="utf-8")
    assert resolve_playbook_file(ws, "PLAN.md") == root_plan

    # 3. Canonical .sync/playbook/ takes precedence over root when both exist
    playbook_plan = playbook_dir / "PLAN.md"
    playbook_plan.write_text("# Playbook Plan", encoding="utf-8")
    assert resolve_playbook_file(ws, "PLAN.md") == playbook_plan


def test_architect_writable_paths():
    """Verify that the Architect role is permitted to write all playbook locations."""
    assert is_architect_writable_path(".sync/playbook/ARCHITECTURE.md") is True
    assert is_architect_writable_path(".sync/playbook/DESIGN.md") is True
    assert is_architect_writable_path(".sync/playbook/PLAN.md") is True
    assert is_architect_writable_path("playbook/ARCHITECTURE.md") is True
    assert is_architect_writable_path("playbook/DESIGN.md") is True
    assert is_architect_writable_path("playbook/PLAN.md") is True
    assert is_architect_writable_path("ARCHITECTURE.md") is True
    assert is_architect_writable_path("DESIGN.md") is True
    assert is_architect_writable_path("PLAN.md") is True

    # Application implementation source writes must still be denied
    assert is_architect_writable_path("src/index.js") is False
    assert is_architect_writable_path("tests/test_logic.py") is False


def test_validate_architecture_structure():
    """Verify structural validation of ARCHITECTURE.md."""
    # Valid architecture
    is_valid, errors = validate_architecture_structure(SAMPLE_VALID_ARCHITECTURE)
    assert is_valid is True, f"Validation failed: {errors}"
    assert errors == []

    # Missing Title
    is_valid, errors = validate_architecture_structure("## System Architecture\noverview")
    assert is_valid is False
    assert any("heading" in err.lower() for err in errors)

    # Missing Tech Stack
    missing_stack = "# Architecture: Test\n\n## System Architecture\noverview\n\n## Directory & Module Structure\nmodules"
    is_valid, errors = validate_architecture_structure(missing_stack)
    assert is_valid is False
    assert any("tech stack" in err.lower() for err in errors)


def test_validate_design_structure():
    """Verify structural validation of DESIGN.md."""
    # Valid design
    is_valid, errors = validate_design_structure(SAMPLE_VALID_DESIGN)
    assert is_valid is True, f"Validation failed: {errors}"
    assert errors == []

    # Missing UI/UX Theme
    missing_theme = "# Design: Test\n\n## Component Hierarchy & Layout\ngrid\n\n## Interaction & States\nhover"
    is_valid, errors = validate_design_structure(missing_theme)
    assert is_valid is False
    assert any("theme" in err.lower() for err in errors)


def test_validate_playbook_artifacts_full_workspace(tmp_path: Path):
    """Verify validate_playbook_artifacts checks all three mandatory files."""
    ws = tmp_path / "proj"
    playbook_dir = ws / ".sync" / "playbook"
    playbook_dir.mkdir(parents=True)

    # Initially all missing
    results = validate_playbook_artifacts(ws)
    assert results["ARCHITECTURE.md"][0] is False
    assert results["DESIGN.md"][0] is False
    assert results["PLAN.md"][0] is False

    # Populate valid files
    (playbook_dir / "ARCHITECTURE.md").write_text(SAMPLE_VALID_ARCHITECTURE, encoding="utf-8")
    (playbook_dir / "DESIGN.md").write_text(SAMPLE_VALID_DESIGN, encoding="utf-8")
    (playbook_dir / "PLAN.md").write_text(SAMPLE_VALID_PLAN, encoding="utf-8")

    results_after = validate_playbook_artifacts(ws)
    assert results_after["ARCHITECTURE.md"][0] is True
    assert results_after["DESIGN.md"][0] is True
    assert results_after["PLAN.md"][0] is True


def test_worker_task_contract_grounding(tmp_path: Path):
    """Worker task contracts must inject architecture_grounding, design_grounding, and plan_overview."""
    ws = tmp_path / "proj"
    playbook_dir = ws / ".sync" / "playbook"
    playbook_dir.mkdir(parents=True)

    (playbook_dir / "ARCHITECTURE.md").write_text(SAMPLE_VALID_ARCHITECTURE, encoding="utf-8")
    (playbook_dir / "DESIGN.md").write_text(SAMPLE_VALID_DESIGN, encoding="utf-8")
    (playbook_dir / "PLAN.md").write_text(SAMPLE_VALID_PLAN, encoding="utf-8")

    wo_data = {
        "id": "WO-007",
        "title": "Implement UI Styling",
        "deliverable": {"path": "assets/css/style.css"},
    }
    contract_data = {
        "work_order": "WO-007",
        "agent_id": "gemini",
        "identity": {"role": "frontend"},
    }

    contract = compile_worker_task_contract(
        task=None,
        wo_data=wo_data,
        contract_data=contract_data,
        workspace=ws,
    )

    assert "project_plan_overview" in contract
    assert "web-calculator" in contract["project_plan_overview"]
    assert "architecture_grounding" in contract
    assert "Tech Stack & Dependencies" in contract["architecture_grounding"]
    assert "design_grounding" in contract
    assert "UI/UX Design Specification Reference" in contract["design_grounding"]

    rendered_sys = render_worker_system(contract)
    assert ".sync/playbook/ARCHITECTURE.md" in rendered_sys
    assert ".sync/playbook/DESIGN.md" in rendered_sys


def test_planning_core_tool_names_include_skills_and_search():
    """Architect's planning tools must include skill retrieval and web search, but exclude delegation."""
    assert "skill_retrieve" in PLANNING_CORE_TOOL_NAMES
    assert "skill_list" in PLANNING_CORE_TOOL_NAMES
    assert "web_search" in PLANNING_CORE_TOOL_NAMES
    assert "web_fetch" in PLANNING_CORE_TOOL_NAMES
    assert "image_search" in PLANNING_CORE_TOOL_NAMES
    assert "delegate_task" not in PLANNING_CORE_TOOL_NAMES


def test_planning_permission_directive_scopes_to_playbook():
    """Planning phase prompt must strictly direct architect to playbook artifacts."""
    from validators.kernel.permission_matrix import get_permission_matrix_prompt

    prompt = get_permission_matrix_prompt("claude", phase="planning")
    assert "PLANNING PHASE / TURN 1" in prompt
    assert ".sync/playbook/ARCHITECTURE.md" in prompt
    assert ".sync/playbook/DESIGN.md" in prompt
    assert ".sync/playbook/PLAN.md" in prompt
    assert "DO NOT WRITE WORK ORDERS OR CONTRACTS IN THIS TURN" in prompt
    assert "Do NOT attempt to delegate tasks before plan approval" in prompt


def test_work_order_alias_and_deliverable_normalization():
    """Work orders with shorthand agent and missing deliverable description must normalize."""
    from validators.harness.authoring_yaml import normalize_work_order_data
    from validators.harness.authoring_gate import AuthoringGate

    raw = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Project Scaffolding",
        "status": "READY",
        "priority": "P1",
        "dependencies": [],
        "agent": "codex",
        "deliverable": {
            "path": "package.json",
            "type": "config",
        },
    }
    normalized = normalize_work_order_data(raw)
    assert normalized["assigned_agents"] == ["codex"]
    assert normalized["deliverable"]["description"] == "Deliverable for Project Scaffolding"
    assert normalized["dependencies"] == []
    assert normalized["priority"] == "P1"
    assert normalized["status"] == "READY"

    gate = AuthoringGate()
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(raw),
        agent="claude",
    )
    assert decision.passed is True, f"Validation failed: {decision.errors}"


def test_index_yaml_normalization_and_gate_validation():
    """INDEX.yaml missing schema_version, next_id, and using work_orders must normalize and pass."""
    from validators.harness.authoring_gate import AuthoringGate

    raw_index = {
        "work_orders": [
            {
                "id": "WO-001",
                "title": "Scaffolding",
                "status": "READY",
                "agent": "codex",
                "deliverable": {"path": "package.json", "type": "config"},
            }
        ]
    }
    gate = AuthoringGate()
    decision = gate.validate_artifact_content(
        ".sync/work-orders/INDEX.yaml",
        yaml.safe_dump(raw_index),
        agent="claude",
    )
    assert decision.passed is True, f"Validation failed: {decision.errors}"
    assert decision.normalized_content is not None
    loaded = yaml.safe_load(decision.normalized_content)
    assert loaded["schema_version"] == 1
    assert loaded["next_id"] == 2
    assert "orders" in loaded
    assert len(loaded["orders"]) == 1
    assert loaded["orders"][0]["assigned_agents"] == ["codex"]


def test_parse_plan_heading_style_milestones_and_task_lists():
    """parse_plan must parse heading-style milestones (### Milestone 1: ...) and subtasks."""
    from validators.harness.plan import parse_plan

    plan_md = """# Project Plan: Secure Auth System

## Current Architecture
Node.js + Express backend with TypeScript and SQLite.

## Milestones & Roadmap

### Milestone 1: Scaffolding & Config (Agent: codex)
- [ ] Task 1.1: Init package.json
- [ ] Task 1.2: Configure TypeScript

### Milestone 2: Database Schema (Agent: codex)
- [ ] Task 2.1: Implement db connection
- [ ] Task 2.2: Drizzle schema

### Milestone 3: Frontend Client (Agent: gemini)
- [ ] Task 3.1: Create design tokens

## Risk Mitigation
| Risk | Impact |
| High | High |

## Agent Assignment Summary
| Agent | Milestones | Focus Area |
| codex | 1, 2 | Backend |
| gemini | 3 | Frontend |
"""
    parsed = parse_plan(plan_md)
    assert len(parsed.milestones) == 3
    assert parsed.milestones[0].id == "Milestone 1"
    assert parsed.milestones[0].title == "Scaffolding & Config"
    assert parsed.milestones[0].agent == "codex"
    assert len(parsed.milestones[0].tasks) == 2

    assert parsed.milestones[1].id == "Milestone 2"
    assert parsed.milestones[1].title == "Database Schema"
    assert parsed.milestones[1].agent == "codex"
    assert len(parsed.milestones[1].tasks) == 2

    assert parsed.milestones[2].id == "Milestone 3"
    assert parsed.milestones[2].title == "Frontend Client"
    assert parsed.milestones[2].agent == "gemini"
    assert len(parsed.milestones[2].tasks) == 1


def test_readiness_recognizes_completed_work_orders_and_superseded_deps(tmp_path: Path):
    """validate_authoring_readiness must recognize orders with completion as completed."""
    from validators.harness.authoring_readiness import validate_authoring_readiness

    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / ".sync" / "playbook").mkdir(parents=True)
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    (ws / ".sync" / "work-orders" / "COMPLETED").mkdir(parents=True)
    (ws / ".sync" / "work-orders" / "CANCELLED").mkdir(parents=True)
    (ws / ".sync" / "contracts").mkdir(parents=True)

    # Completed order with completion payload sitting in ACTIVE/
    completed_wo = {
        "id": "WO-018",
        "title": "Scaffolding",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": ["WO-017"],
        "deliverable": {"path": "package.json", "type": "config"},
        "completion": {"verified": True},
    }
    (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-018.yaml").write_text(yaml.safe_dump(completed_wo), encoding="utf-8")

    # Active order depending on WO-018
    active_wo = {
        "id": "WO-096",
        "title": "DB Migrations",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": ["WO-018"],
        "deliverable": {"path": "src/db.ts", "type": "code"},
        "test_plan": [{"source": "src/db.ts", "tests": ["tests/test_db.py"]}],
    }
    (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-096.yaml").write_text(yaml.safe_dump(active_wo), encoding="utf-8")

    contract = {
        "work_order": "WO-096",
        "agent_id": "codex",
        "budget": {"max_files_touched": 5},
        "scope": {"allow": ["src/db.ts", "tests/test_db.py"]},
    }
    (ws / ".sync" / "contracts" / "WO-096.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")

    res = validate_authoring_readiness(ws, superseded_wo_ids=["WO-017"])
    assert res.ready is True, f"Expected readiness to pass, got issues: {[i.message for i in res.issues]}"


def test_reconcile_promotes_completed_orders_even_without_plan_milestones(tmp_path: Path):
    """reconcile_active_work_orders_with_plan must promote completed orders even if no milestones."""
    from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan

    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    (ws / ".sync" / "work-orders" / "COMPLETED").mkdir(parents=True)
    (ws / ".sync" / "work-orders" / "CANCELLED").mkdir(parents=True)

    completed_wo = {
        "id": "WO-055",
        "title": "Auth API",
        "status": "ACTIVE",
        "deliverable": {"path": "src/auth.ts", "type": "code"},
        "completion": {"outcome": "success"},
    }
    (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-055.yaml").write_text(yaml.safe_dump(completed_wo), encoding="utf-8")

    reconciled = reconcile_active_work_orders_with_plan(ws)
    assert "WO-055" in reconciled
    assert not (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-055.yaml").is_file()
    assert (ws / ".sync" / "work-orders" / "COMPLETED" / "WO-055.yaml").is_file()

