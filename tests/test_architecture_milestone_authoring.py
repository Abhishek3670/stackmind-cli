from pathlib import Path
import tempfile
import yaml

from validators.harness.plan import parse_plan
from validators.harness.authoring_readiness import (
    load_plan_milestones,
    load_expected_milestones,
    _is_architecture_or_meta_milestone,
    validate_authoring_readiness,
)
from validators.kernel.daemon.authoring import decompose_plan_milestones


def test_architecture_milestone_detection():
    plan_text = """# Project Plan: Sample App

## Current Architecture
Sample architecture.

## Milestones & Roadmap
- [ ] Milestone 0: Architecture & Work-Order Scaffolding (Agent: claude) — *completed by this document + WO-000*
  - [ ] Task 0.1: Author PLAN.md
- [ ] Milestone 1: Frontend Scaffolding (Agent: gemini)
  - [ ] Task 1.1: Build index.html
"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "PLAN.md").write_text(plan_text, encoding="utf-8")

        parsed = parse_plan(plan_text)
        assert len(parsed.milestones) == 2
        m0 = parsed.milestones[0]
        assert m0.agent == "claude"
        assert _is_architecture_or_meta_milestone(m0) is True

        m1 = parsed.milestones[1]
        assert m1.agent == "gemini"
        assert _is_architecture_or_meta_milestone(m1) is False

        expected = load_expected_milestones(ws)
        assert expected is not None
        # Milestone 0 must NOT be in expected implementation milestones
        assert len(expected) == 1
        assert "Frontend Scaffolding" in expected[0]

        # Decomposing plan milestones must NOT generate a worker work order for Milestone 0
        wos = decompose_plan_milestones(parsed.milestones, start_idx=1)
        assert len(wos) >= 1
        for wo in wos:
            assert "claude" not in wo.get("assigned_agents", [])
            assert wo.get("milestone_id") != "Milestone 0"
