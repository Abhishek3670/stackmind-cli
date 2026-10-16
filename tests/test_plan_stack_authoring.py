"""Phase 1 Tests: Approved Plan Stack Authoritativeness (SDP-FIX-001).

Validates:
1. Node.js/Express/React plan produces stack-compatible work orders (package.json,
   JS/TS backend, React frontend, .test.js QA test suites; zero .py deliverables).
2. Python/Flask plan produces Python-compatible work orders (requirements.txt,
   src/backend.py, test_*.py; zero .js/.jsx files).
3. Explicit deliverable paths in plan milestones override default/generic conventions.
4. Existing project structure on disk (e.g. package.json) is respected when plan is terse.
5. Contradictory / ambiguous stack specifications fail closed with PlanValidationError.
6. Companion test resolution in D024 gate accurately detects JS/TS test suites.
"""

from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from validators.harness.plan import (
    parse_plan,
    parse_tech_stack,
    TechStack,
    PlanValidationError,
)
from validators.kernel.daemon.authoring import (
    extract_deliverable_spec,
    synthesize_child_work_orders,
    derive_implementation_estimate,
)
from validators.harness.d024_gate import D024Gate


def test_nodejs_express_react_plan_produces_stack_compatible_work_orders(tmp_path: Path) -> None:
    """A plan specifying Node.js, Express, and React must NOT generate any Python deliverables."""
    plan_content = """# Project Plan: Modern SaaS Web Application

## Current Architecture
A modern web application built on Node.js and Express for the REST API backend,
with a React frontend and PostgreSQL database.

Stack & Dependencies:
- Backend: Node.js, Express.js
- Frontend: React
- Database: PostgreSQL

## Milestones & Roadmap
- [ ] Milestone 1: Project Scaffolding & Dependencies (Agent: codex)
  - [ ] Initialize project configuration and third-party dependencies
- [ ] Milestone 2: User Authentication API (Agent: codex)
  - [ ] Implement user registration, login, and JWT token validation
- [ ] Milestone 3: Landing Page & Dashboard UI (Agent: gemini)
  - [ ] Build landing page with hero banner and auth forms
- [ ] Milestone 4: End-to-End QA Test Suite (Agent: gemma)
  - [ ] Author and execute integration test suite
- [ ] Milestone 5: GitOps Release & Packaging (Agent: local-llm)
  - [ ] Tag release commit and generate changelog
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    # 1. Verify tech stack parsing
    parsed = parse_plan(plan_content, project_path=tmp_path)
    stack = parsed.tech_stack
    assert stack is not None
    assert stack.backend_language in ("javascript", "typescript")
    assert stack.backend_framework == "express"
    assert stack.frontend_framework == "react"
    assert stack.database == "postgresql"
    assert stack.dependency_manifest == "package.json"
    assert not stack.is_ambiguous

    # 2. Synthesize work orders and contracts
    records = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(records) == 5

    deliverable_paths = [r["deliverable"]["path"] for r in records]
    
    # Milestone 1 (Scaffolding): package.json
    assert records[0]["deliverable"]["path"] == "package.json"
    assert records[0]["deliverable"]["type"] == "config"

    # Milestone 2 (Backend API): JavaScript/TypeScript file (NOT src/backend.py)
    assert records[1]["deliverable"]["path"] in ("src/routes/auth.js", "src/server.js")
    assert records[1]["deliverable"]["type"] == "code"
    assert not records[1]["deliverable"]["path"].endswith(".py")

    # Milestone 3 (Frontend): React component (NOT src/landing.html)
    assert records[2]["deliverable"]["path"] in ("src/components/Landing.jsx", "src/App.jsx")
    assert records[2]["deliverable"]["type"] == "code"

    # Milestone 4 (QA): JavaScript test suite (NOT .py)
    assert records[3]["deliverable"]["path"].endswith(".test.js")
    assert records[3]["deliverable"]["type"] == "code"

    # Milestone 5 (GitOps): VERSION.md or package.json
    assert records[4]["deliverable"]["path"] in ("VERSION.md", "CHANGELOG.md", "package.json")

    # Crucial assertion: ZERO Python files anywhere in deliverables or implementation estimates!
    for r in records:
        path = r["deliverable"].get("path") or ""
        assert not path.endswith(".py"), f"Found unexpected python deliverable: {path}"
        est = r.get("implementation_estimate")
        if est:
            for exp in est.get("expected_files", []):
                assert not exp.endswith(".py"), f"Found unexpected python expected file: {exp}"


def test_python_flask_plan_produces_python_work_orders(tmp_path: Path) -> None:
    """A plan specifying Python and Flask correctly produces Python deliverables."""
    plan_content = """# Project Plan: Flask Analytics API

## Current Architecture
Lightweight analytics backend built with Python, Flask, and SQLite.

Stack & Dependencies:
- Backend: Python 3.12, Flask
- Database: SQLite

## Milestones & Roadmap
- [ ] Milestone 1: Setup dependencies (Agent: codex)
  - [ ] Initialize requirements.txt
- [ ] Milestone 2: Analytics Event Ingestion API (Agent: codex)
  - [ ] Implement ingest endpoint and SQLite storage
- [ ] Milestone 3: QA Verification Suite (Agent: gemma)
  - [ ] Author and run test suite for analytics endpoint
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    parsed = parse_plan(plan_content, project_path=tmp_path)
    stack = parsed.tech_stack
    assert stack is not None
    assert stack.backend_language == "python"
    assert stack.backend_framework == "flask"
    assert stack.database == "sqlite"
    assert stack.dependency_manifest == "requirements.txt"

    records = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(records) == 3

    # Milestone 1: requirements.txt
    assert records[0]["deliverable"]["path"] == "requirements.txt"

    # Milestone 2: src/backend.py
    assert records[1]["deliverable"]["path"] == "src/backend.py"

    # Milestone 3: tests/test_*.py
    assert records[2]["deliverable"]["path"].endswith(".py")
    assert "test" in records[2]["deliverable"]["path"]


def test_explicit_artifact_paths_override_generic_inference(tmp_path: Path) -> None:
    """Explicit file paths declared by the Architect in milestone tasks override defaults."""
    plan_content = """# Project Plan: Custom Microservice

## Current Architecture
Node.js Express microservice with specialized directory layout.

## Milestones & Roadmap
- [ ] Milestone 1: Implement custom user service (Agent: codex)
  - [ ] Write logic in `services/custom_user_service.js`
  - [ ] Write integration test in `tests/custom_user_test.js`
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    records = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(records) == 1
    wo = records[0]

    # Explicit code path services/custom_user_service.js must be respected
    assert wo["deliverable"]["path"] == "services/custom_user_service.js"
    assert "services/custom_user_service.js" in wo["implementation_estimate"]["expected_files"]
    assert "tests/custom_user_test.js" in wo["implementation_estimate"]["expected_files"]


def test_existing_project_configuration_on_disk_is_respected(tmp_path: Path) -> None:
    """If project on disk has package.json, authoring infers Node.js even with a terse plan."""
    (tmp_path / "package.json").write_text(
        '{"name": "test-node-app", "dependencies": {"express": "^4.19.0"}}',
        encoding="utf-8",
    )

    plan_content = """# Project Plan: Application Extension

## Current Architecture
Adding features to existing repository.

## Milestones & Roadmap
- [ ] Milestone 1: Implement authentication routes (Agent: codex)
  - [ ] Add auth endpoints
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    parsed = parse_plan(plan_content, project_path=tmp_path)
    assert parsed.tech_stack is not None
    assert parsed.tech_stack.backend_language in ("javascript", "typescript")
    assert parsed.tech_stack.dependency_manifest == "package.json"

    records = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(records) == 1
    # Must NOT produce .py because package.json is on disk
    assert not records[0]["deliverable"]["path"].endswith(".py")
    assert records[0]["deliverable"]["path"].endswith((".js", ".ts"))


def test_contradictory_stack_fails_closed(tmp_path: Path) -> None:
    """A plan with contradictory backend stacks (e.g. Node + Python) must fail closed."""
    plan_content = """# Project Plan: Conflicting Stack Project

## Current Architecture
Backend Stack: Node.js and Python Flask microservices combined ambiguously.

## Milestones & Roadmap
- [ ] Milestone 1: Backend service (Agent: codex)
  - [ ] Implement core logic
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    parsed = parse_plan(plan_content, project_path=tmp_path)
    assert parsed.tech_stack is not None
    assert parsed.tech_stack.is_ambiguous
    assert "Conflicting" in parsed.tech_stack.ambiguity_reason

    # Synthesize must fail closed with PlanValidationError
    with pytest.raises(PlanValidationError) as exc_info:
        synthesize_child_work_orders(tmp_path, plan_content)
    assert "ambiguous technology stack" in str(exc_info.value).lower()


def test_d024_gate_companion_test_resolution_for_javascript(tmp_path: Path) -> None:
    """D024Gate accurately identifies companion test suites for JavaScript deliverables."""
    gate = D024Gate()

    # Deliverable: src/server.js
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    server_file = tmp_path / "src" / "server.js"
    server_file.write_text("console.log('server');", encoding="utf-8")

    # Companion test: tests/server.test.js
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    test_file = tmp_path / "tests" / "server.test.js"
    test_file.write_text("test('server starts', () => {});", encoding="utf-8")

    companion = gate.find_companion_test_file(tmp_path, "src/server.js")
    assert companion is not None
    assert companion.resolve() == test_file.resolve()
