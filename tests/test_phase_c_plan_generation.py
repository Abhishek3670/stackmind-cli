"""Tests for Phase C: PLAN.md generation, structural validation, and downstream usability."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import pytest
import yaml

from cli.init import init
from validators.harness.plan import (
    PlanMilestone,
    PlanStructure,
    PlanValidationError,
    parse_plan,
    validate_plan_file,
    validate_plan_structure,
)
from validators.harness.runner import (
    AgentRunner,
    HarnessTask,
)
from validators.kernel.providers import OpenAICompatibleAdapter


VALID_PLAN_CONTENT = """# Project Plan: autonomous-agent-runtime

This document serves as the living architectural roadmap for the autonomous agent runtime.

## Current Architecture
- Kernel runtime with strict contract and identity boundaries (CONTRACT-01).
- ToolGateway with sandboxed file and command execution.
- Harness runner with staged verification dimensions and audit logging.

## Milestones & Roadmap
- [ ] Milestone 1: Core Kernel Scaffolding
  - [ ] Implement scratch workspace isolation
  - [ ] Add runtime boundary and journal
- [x] Milestone 2: Governed Tool Calling
  - [x] Integrate ProviderGateway and ToolGateway
- [ ] Milestone 3: Autonomous Pipeline Integration
"""

INVALID_PLAN_MISSING_ARCH = """# Project Plan: broken-plan

## Milestones & Roadmap
- [ ] Milestone 1: Initial Setup
"""

INVALID_PLAN_MISSING_MILESTONES = """# Project Plan: broken-plan

## Current Architecture
- Just some architecture with no milestones.
"""

INVALID_PLAN_MISSING_CHECKLIST = """# Project Plan: broken-plan

## Current Architecture
- Some architecture notes.

## Milestones & Roadmap
There are no tasks or milestones listed as checkboxes here.
"""


def test_validate_plan_structure_valid():
    is_valid, errors = validate_plan_structure(VALID_PLAN_CONTENT)
    assert is_valid is True
    assert errors == []


def test_validate_plan_structure_missing_architecture():
    is_valid, errors = validate_plan_structure(INVALID_PLAN_MISSING_ARCH)
    assert is_valid is False
    assert any("Architecture" in err for err in errors)


def test_validate_plan_structure_missing_milestones():
    is_valid, errors = validate_plan_structure(INVALID_PLAN_MISSING_MILESTONES)
    assert is_valid is False
    assert any("Milestones" in err or "Roadmap" in err for err in errors)


def test_validate_plan_structure_missing_checklist():
    is_valid, errors = validate_plan_structure(INVALID_PLAN_MISSING_CHECKLIST)
    assert is_valid is False
    assert any("checklist item" in err for err in errors)


def test_validate_plan_structure_empty():
    is_valid, errors = validate_plan_structure("")
    assert is_valid is False
    assert any("empty" in err for err in errors)


def test_parse_plan_downstream_usability():
    plan = parse_plan(VALID_PLAN_CONTENT)
    assert isinstance(plan, PlanStructure)
    assert plan.project_name == "autonomous-agent-runtime"
    assert "Kernel runtime" in plan.architecture_overview

    assert len(plan.milestones) == 3
    m1 = plan.milestones[0]
    assert m1.id == "Milestone 1"
    assert m1.title == "Core Kernel Scaffolding"
    assert m1.status == "PENDING"
    assert len(m1.tasks) == 2
    assert "Implement scratch workspace isolation" in m1.tasks[0]

    m2 = plan.milestones[1]
    assert m2.id == "Milestone 2"
    assert m2.status == "COMPLETED"

    # Test dictionary export
    exported = plan.to_dict()
    assert exported["project_name"] == "autonomous-agent-runtime"
    assert len(exported["milestones"]) == 3
    assert plan.get_milestone("Milestone 1") is not None


def test_parse_plan_rejects_invalid():
    with pytest.raises(PlanValidationError):
        parse_plan(INVALID_PLAN_MISSING_ARCH)


def _setup_architecture_project(tmp_path: Path, work_order_id: str = "WO-030") -> Path:
    project = tmp_path / "project"
    init(project, name="PhaseCArch", no_git=True)
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["claude"]["assigned_work_orders"] = [work_order_id]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    (project / ".sync" / "work-orders" / "ACTIVE" / f"{work_order_id}.yaml").write_text(
        yaml.safe_dump({
            "id": work_order_id,
            "type": "FEATURE",
            "title": "Generate living architectural roadmap PLAN.md",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["claude"],
            "dependencies": [],
            "deliverable": {
                "type": "doc",
                "path": "PLAN.md",
                "description": "Living architectural roadmap",
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    index_path = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    index_data["orders"].append({
        "id": work_order_id,
        "title": "Generate living architectural roadmap PLAN.md",
        "status": "ACTIVE",
        "priority": "P0",
        "dependencies": [],
        "deliverable": {
            "type": "doc",
            "path": "PLAN.md",
            "description": "Living architectural roadmap",
        },
    })
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / f"{work_order_id}.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": work_order_id,
            "identity": {
                "role": "Senior Architect",
                "reports_to": "ceo",
            },
            "scope": {
                "allow": [{"module": "PLAN.md"}],
                "deny": [{"module": "src"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 10000},
        }, sort_keys=False),
        encoding="utf-8",
    )
    return project


def test_phase_c_claude_generates_valid_plan_end_to_end(tmp_path: Path):
    """Verify with a real Architecture-agent run that PLAN.md is generated through the governed tool loop,

    written to disk, verified, and structurally usable downstream.
    """
    project = _setup_architecture_project(tmp_path, work_order_id="WO-030")
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        messages = payload["messages"]
        if calls == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "write_plan",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "PLAN.md", "content": VALID_PLAN_CONTENT}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        if calls == 2:
            # Confirm tool returned success
            assert any(m.get("role") == "tool" for m in messages)
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "read_plan",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "PLAN.md"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "status": "completed",
                        "summary": "Generated PLAN.md living architectural roadmap",
                        "report_markdown": "Architectural roadmap created and verified.",
                        "blockers": [],
                        "modified_files": ["PLAN.md"],
                        "release_target": "v3.1.0",
                        "retrieval_queries": [],
                        "uncertainty": [],
                        "commands": [],
                    }),
                },
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 50},
        }

    runner = AgentRunner(project, "claude", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    # 1. Verification passed
    assert result.status == "completed", result.reason

    # 2. File was promoted to live workspace on disk
    live_plan = project / "PLAN.md"
    assert live_plan.exists(), "PLAN.md was not written to live project root"
    disk_content = live_plan.read_text(encoding="utf-8")
    assert "autonomous-agent-runtime" in disk_content

    # 3. Downstream structural usability
    parsed = parse_plan(disk_content)
    assert parsed.project_name == "autonomous-agent-runtime"
    assert len(parsed.milestones) == 3
    assert parsed.get_milestone("Milestone 1") is not None
    assert parsed.get_milestone("Milestone 2").status == "COMPLETED"


def test_phase_c_claude_malformed_plan_rejected_fail_closed(tmp_path: Path):
    """Verify that a malformed PLAN.md is rejected fail-closed and not promoted."""
    project = _setup_architecture_project(tmp_path, work_order_id="WO-030")
    # Clean out template PLAN.md if present
    existing_plan = project / "PLAN.md"
    if existing_plan.exists():
        existing_plan.unlink()

    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "write_bad_plan",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "PLAN.md", "content": INVALID_PLAN_MISSING_ARCH}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "status": "completed",
                        "summary": "Wrote malformed plan",
                        "report_markdown": "done",
                        "blockers": [],
                        "modified_files": ["PLAN.md"],
                        "release_target": "v3.1.0",
                        "retrieval_queries": [],
                        "uncertainty": [],
                        "commands": [],
                    }),
                },
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 50},
        }

    runner = AgentRunner(project, "claude", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    # Staged verification must fail fail-closed
    assert result.status == "blocked"
    assert "verification" in result.reason.lower() or "dimensions" in result.reason.lower()

    # Live file must NOT exist (changes rejected and blocked from promotion)
    assert not (project / "PLAN.md").exists(), "Malformed PLAN.md should not have been promoted to live project root"
