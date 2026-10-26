"""Tests for tech stack alternative parsing and authoring gate YAML/schema resilience."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from validators.harness.plan import parse_tech_stack, parse_plan
from validators.harness.authoring_gate import AuthoringGate


class TestTechStackAlternatives:
    """Test that alternative patterns (e.g. 'Node.js/Python (choose one)') do not fail fail-closed as ambiguous."""

    def test_choose_one_alternative_resolves_without_ambiguity(self):
        plan_content = """# Project Plan: Landing App
## Architecture
- Backend: Node.js/Python (choose one), Express/FastAPI, PostgreSQL for booking
- Frontend: React

## Milestones & Roadmap
- [ ] Milestone 1: Setup
"""
        stack = parse_tech_stack(plan_content)
        assert not stack.is_ambiguous, f"Expected not ambiguous, got: {stack.ambiguity_reason}"
        assert stack.backend_language in ("javascript", "typescript", "python")

    def test_or_separator_resolves_without_ambiguity(self):
        plan_content = """# Project Plan: Landing App
## Architecture
- Backend: Python or Node.js Express
- Frontend: React

## Milestones & Roadmap
- [ ] Milestone 1: Setup
"""
        stack = parse_tech_stack(plan_content)
        assert not stack.is_ambiguous, f"Expected not ambiguous, got: {stack.ambiguity_reason}"
        assert stack.backend_language == "python"

    def test_slash_separated_alternatives_resolve_first_listed(self):
        plan_content = """# Project Plan: Landing App
## Architecture
- Backend: Python/Node.js
- Frontend: React

## Milestones & Roadmap
- [ ] Milestone 1: Setup
"""
        stack = parse_tech_stack(plan_content)
        assert not stack.is_ambiguous, f"Expected not ambiguous, got: {stack.ambiguity_reason}"
        assert stack.backend_language == "python"

    def test_explicit_conflicting_specification_still_fails_closed(self):
        """Genuine conflicting microservices specification must still be detected as ambiguous."""
        plan_content = """# Project Plan: Conflicting App
## Architecture
- Backend: Python FastAPI and Node.js Express combined microservices conflicting
- Frontend: React

## Milestones & Roadmap
- [ ] Milestone 1: Setup
"""
        stack = parse_tech_stack(plan_content)
        assert stack.is_ambiguous
        assert "Conflicting" in stack.ambiguity_reason


class TestAuthoringGateResilience:
    """Test that AuthoringGate recovers from common LLM YAML glitches."""

    def test_repair_block_scalar_indentation(self):
        broken_yaml = """id: "WO-004"
milestone_id: "Milestone 4"
type: "FEATURE"
title: "Deployment & GitOps"
status: "ACTIVE"
priority: "P3"
assigned_agents:
  - "local-llm"
dependencies: ["WO-003"]
deliverable:
  type: "config"
  path: "docker-compose.yml"
  description: "Docker orchestration configuration"
description: >
  Tasks:
  1. Task one
  2. Task two
 3. Inconsistently indented task
 4. Another inconsistently indented task
"""
        gate = AuthoringGate()
        decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-004.yaml", broken_yaml, agent="claude")
        assert decision.passed, f"Expected passed, got errors: {decision.errors}"
        assert decision.normalized_content is not None
        # Verify valid yaml now
        loaded = yaml.safe_load(decision.normalized_content)
        assert loaded["id"] == "WO-004"
        assert "Inconsistently indented task" in loaded["description"]

    def test_normalize_misplaced_acceptance_criteria_in_deliverable(self):
        yaml_with_nested_criteria = """id: "WO-001"
milestone_id: "Milestone 1"
type: "FEATURE"
title: "Configure Project Dependencies"
status: "ACTIVE"
priority: "P0"
assigned_agents:
  - "codex"
dependencies: []
deliverable:
  type: "config"
  path: "requirements.txt"
  description: "Project dependency manifest"
  acceptance_criteria:
    - "requirements.txt created with all dependencies"
    - "dev dependencies included"
description: "Setup project dependencies"
"""
        gate = AuthoringGate()
        decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", yaml_with_nested_criteria, agent="claude")
        assert decision.passed, f"Expected passed, got errors: {decision.errors}"
        loaded = yaml.safe_load(decision.normalized_content)
        assert "acceptance_criteria" not in loaded["deliverable"], "acceptance_criteria should have been removed from deliverable"
        assert "acceptance_criteria" in loaded, "acceptance_criteria should be moved to top level"
        assert len(loaded["acceptance_criteria"]) == 2
