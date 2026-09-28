"""Tests for Dependency & Import Satisfiability Gate.

Covers:
- Standard-library imports (pass without manifest)
- Local project imports (root, src, lib, sibling, relative)
- Correctly declared third-party dependencies (pyproject.toml, requirements*.txt, poetry)
- Undeclared Flask/Werkzeug-style dependencies
- Import/distribution name mismatches (yaml->pyyaml, bs4->beautifulsoup4, etc.)
- Missing dependency manifests (fail closed when external imports present)
- Multiple deliverables
- Deliberately failing cases & integration with D024Gate and AgentRunner
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from validators.harness.d024_gate import D024Gate
from validators.harness.dependency_gate import (
    ImportSatisfiabilityResult,
    check_import_satisfiability,
    check_multiple_deliverables,
    extract_top_level_imports,
    is_import_declared,
    is_local_module,
    is_manifest_permitted_by_contract,
    is_standard_library,
    normalize_distribution_name,
    read_project_dependencies,
)
from validators.harness.runner import AgentRunner, HarnessDecision, HarnessTask
from validators.harness.snapshot import WorkspaceDiff, WorkspaceSnapshot


# ─── 1. Standard-Library Imports ──────────────────────────────────────────


def test_standard_library_imports_pass_without_manifest(tmp_path: Path):
    """Deliverable using only standard library modules passes even when no manifest exists."""
    project = tmp_path / "project"
    project.mkdir()
    deliv = project / "script.py"
    deliv.write_text(
        "import os\n"
        "import sys\n"
        "import json\n"
        "from pathlib import Path\n"
        "import hashlib, math\n"
        "import unittest\n\n"
        "def run():\n"
        "    return os.path.exists(sys.executable)\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()
    assert result.undeclared_imports == ()
    assert result.diagnostic is None


def test_standard_library_future_and_builtins(tmp_path: Path):
    """__future__, builtins, and typing modules are recognized as standard library."""
    project = tmp_path / "project"
    project.mkdir()
    deliv = project / "typed_module.py"
    deliv.write_text(
        "from __future__ import annotations\n"
        "import builtins\n"
        "from typing import Any, Optional, Union\n"
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Config:\n"
        "    name: str\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()


def test_is_standard_library_helper():
    assert is_standard_library("os") is True
    assert is_standard_library("sys") is True
    assert is_standard_library("json") is True
    assert is_standard_library("ast") is True
    assert is_standard_library("collections") is True
    assert is_standard_library("__future__") is True
    assert is_standard_library("flask") is False
    assert is_standard_library("werkzeug") is False
    assert is_standard_library("requests") is False


# ─── 2. Local Project Imports ─────────────────────────────────────────────


def test_local_project_imports_root_modules(tmp_path: Path):
    """Deliverable importing project-root modules and packages passes."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "helper.py").write_text("def assist(): pass\n", encoding="utf-8")
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# package\n", encoding="utf-8")
    (pkg / "service.py").write_text("class Service: pass\n", encoding="utf-8")

    deliv = project / "main.py"
    deliv.write_text(
        "import helper\n"
        "import pkg\n"
        "from pkg.service import Service\n\n"
        "def main():\n"
        "    helper.assist()\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()


def test_local_project_imports_src_layout(tmp_path: Path):
    """Deliverable within src/ layout importing other src packages passes."""
    project = tmp_path / "project"
    src = project / "src"
    api = src / "api"
    models = src / "models"
    api.mkdir(parents=True)
    models.mkdir(parents=True)

    (models / "__init__.py").write_text("", encoding="utf-8")
    (models / "user.py").write_text("class User: pass\n", encoding="utf-8")

    deliv = api / "auth.py"
    deliv.write_text(
        "from src.models.user import User\n"
        "from models.user import User as UserModel\n\n"
        "def login(): pass\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()


def test_local_project_relative_imports(tmp_path: Path):
    """Relative imports ('from . import foo', 'from ..bar import baz') are treated as local."""
    project = tmp_path / "project"
    pkg = project / "mypkg" / "sub"
    pkg.mkdir(parents=True)
    deliv = pkg / "handler.py"
    deliv.write_text(
        "from . import sibling\n"
        "from ..parent_module import parent_fn\n\n"
        "def handle(): pass\n",
        encoding="utf-8",
    )

    top_imports = extract_top_level_imports(deliv.read_text(encoding="utf-8"))
    assert top_imports == set()

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()


def test_local_project_sibling_imports(tmp_path: Path):
    """Direct sibling import in a subpackage passes."""
    project = tmp_path / "project"
    pkg = project / "app" / "services"
    pkg.mkdir(parents=True)
    (pkg / "db.py").write_text("class Database: pass\n", encoding="utf-8")

    deliv = pkg / "user_service.py"
    deliv.write_text(
        "import db\n\n"
        "def get_user(): return db.Database()\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.external_imports == ()


# ─── 3. Correctly Declared Third-Party Dependencies ───────────────────────


def test_declared_dependencies_in_pyproject_toml(tmp_path: Path):
    """Dependencies declared in pyproject.toml satisfy deliverable imports."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = [\n'
        '    "requests>=2.28.0",\n'
        '    "fastapi",\n'
        '    "pydantic[email]>=2.0"\n'
        ']\n',
        encoding="utf-8",
    )
    deliv = project / "api.py"
    deliv.write_text(
        "import requests\n"
        "from fastapi import FastAPI\n"
        "import pydantic\n\n"
        "app = FastAPI()\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert set(result.external_imports) == {"requests", "fastapi", "pydantic"}
    assert result.undeclared_imports == ()
    assert result.diagnostic is None


def test_declared_dependencies_in_requirements_txt(tmp_path: Path):
    """Dependencies declared in requirements.txt satisfy deliverable imports."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text(
        "# Production dependencies\n"
        "click>=8.0\n"
        "httpx==0.24.1 # HTTP client\n"
        "pytest>=7.0; python_version >= '3.10'\n",
        encoding="utf-8",
    )
    deliv = project / "cli_tool.py"
    deliv.write_text(
        "import click\n"
        "import httpx\n\n"
        "@click.command()\n"
        "def run(): pass\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert set(result.external_imports) == {"click", "httpx"}
    assert result.undeclared_imports == ()


def test_declared_optional_and_poetry_dependencies(tmp_path: Path):
    """Dependencies in optional-dependencies or tool.poetry are recognized."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1.0"\n'
        '[project.optional-dependencies]\n'
        'test = ["pytest>=7.0", "pytest-cov"]\n'
        '[tool.poetry.dependencies]\n'
        'redis = "^4.0"\n'
        'celery = "^5.2"\n',
        encoding="utf-8",
    )
    deliv = project / "tasks.py"
    deliv.write_text(
        "import redis\n"
        "import celery\n"
        "import pytest\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert set(result.external_imports) == {"redis", "celery", "pytest"}
    assert result.undeclared_imports == ()


# ─── 4. Undeclared Flask / Werkzeug Dependencies ──────────────────────────


def test_undeclared_flask_and_werkzeug_detected(tmp_path: Path):
    """Detects undeclared Flask/Werkzeug imports when project only declares requests."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2.28.0"]\n',
        encoding="utf-8",
    )
    src_api = project / "src" / "api"
    src_api.mkdir(parents=True)
    deliv = src_api / "auth.py"
    deliv.write_text(
        "from flask import Flask, request, jsonify\n"
        "from werkzeug.security import generate_password_hash, check_password_hash\n\n"
        "app = Flask(__name__)\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is False
    assert set(result.undeclared_imports) == {"flask", "werkzeug"}
    assert "src/api/auth.py" in str(result.diagnostic)
    assert "flask" in str(result.diagnostic)
    assert "werkzeug" in str(result.diagnostic)


# ─── 5. Import / Distribution Name Mismatches ─────────────────────────────


def test_known_name_mismatches_resolved_correctly(tmp_path: Path):
    """Conservative mapping resolves import names to PyPI distribution names."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = [\n'
        '    "PyYAML>=6.0",\n'
        '    "python-dotenv>=1.0",\n'
        '    "beautifulsoup4>=4.12",\n'
        '    "Pillow>=10.0",\n'
        '    "opencv-python>=4.8",\n'
        '    "scikit-learn>=1.3",\n'
        '    "PyJWT>=2.8",\n'
        ']\n',
        encoding="utf-8",
    )
    deliv = project / "process.py"
    deliv.write_text(
        "import yaml\n"
        "import dotenv\n"
        "from bs4 import BeautifulSoup\n"
        "from PIL import Image\n"
        "import cv2\n"
        "import sklearn\n"
        "import jwt\n",
        encoding="utf-8",
    )

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is True
    assert result.undeclared_imports == ()
    assert result.diagnostic is None


def test_name_mismatch_fails_if_distribution_not_declared(tmp_path: Path):
    """Import name like yaml fails if PyYAML is not declared."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2.28"]\n',
        encoding="utf-8",
    )
    deliv = project / "config.py"
    deliv.write_text("import yaml\n", encoding="utf-8")

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is False
    assert result.undeclared_imports == ("yaml",)
    assert "yaml" in str(result.diagnostic)


# ─── 6. Missing Dependency Manifests ──────────────────────────────────────


def test_missing_manifest_fails_closed_when_external_imports_exist(tmp_path: Path):
    """If project has NO dependency manifest and imports third-party modules, fails closed."""
    project = tmp_path / "bare_project"
    project.mkdir()
    deliv = project / "client.py"
    deliv.write_text("import requests\n", encoding="utf-8")

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is False
    assert result.manifest_found is False
    assert result.undeclared_imports == ("requests",)
    assert "no dependency manifest (pyproject.toml, requirements*.txt) was found" in str(result.diagnostic)


# ─── 7. Multiple Deliverables ─────────────────────────────────────────────


def test_multiple_deliverables_all_passing(tmp_path: Path):
    """check_multiple_deliverables succeeds when all deliverables have satisfied dependencies."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = ["requests", "click"]\n',
        encoding="utf-8",
    )
    deliv1 = project / "client.py"
    deliv1.write_text("import requests\n", encoding="utf-8")
    deliv2 = project / "cli.py"
    deliv2.write_text("import click\n", encoding="utf-8")

    all_passed, results = check_multiple_deliverables([deliv1, deliv2], project_root=project)
    assert all_passed is True
    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is True


def test_multiple_deliverables_one_failing(tmp_path: Path):
    """check_multiple_deliverables fails overall if any deliverable has undeclared imports."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "testapp"\nversion = "0.1.0"\n'
        'dependencies = ["requests"]\n',
        encoding="utf-8",
    )
    deliv1 = project / "client.py"
    deliv1.write_text("import requests\n", encoding="utf-8")
    deliv2 = project / "server.py"
    deliv2.write_text("import flask\n", encoding="utf-8")

    all_passed, results = check_multiple_deliverables([deliv1, deliv2], project_root=project)
    assert all_passed is False
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[1].undeclared_imports == ("flask",)


# ─── 8. Deliberately Failing Cases & Integration ──────────────────────────


def test_partially_declared_imports_flags_undeclared_only(tmp_path: Path):
    """When some imports are declared and others are not, only undeclared ones are flagged."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    deliv = project / "sync.py"
    deliv.write_text("import requests\nimport aiohttp\n", encoding="utf-8")

    result = check_import_satisfiability(deliv, project_root=project)
    assert result.passed is False
    assert result.undeclared_imports == ("aiohttp",)
    assert "aiohttp" in str(result.diagnostic)
    assert "requests" not in str(result.diagnostic)


def test_d024_gate_integration_blocks_undeclared_import(tmp_path: Path):
    """D024Gate blocks a work order whose deliverable imports undeclared packages."""
    from tests.test_d024_qa_gate import _setup_gate_scenario

    # auth_content uses flask and werkzeug, but project has NO manifest
    auth_content = (
        "from flask import Flask, request, jsonify\n"
        "from werkzeug.security import generate_password_hash\n\n"
        "app = Flask(__name__)\n"
    )
    project = _setup_gate_scenario(
        tmp_path,
        deliverable_content=auth_content,
        verdict_type="APPROVED",
        create_test_file=True,
    )
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is False
    assert decision.verdict_status == "NEEDS_CHANGES"
    assert "no dependency manifest" in str(decision.reason) or "undeclared" in str(decision.reason)


def test_d024_gate_integration_passes_when_dependencies_declared(tmp_path: Path):
    """D024Gate passes when the deliverable's dependencies are declared in pyproject.toml."""
    from tests.test_d024_qa_gate import _setup_gate_scenario

    auth_content = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
    )
    project = _setup_gate_scenario(
        tmp_path,
        deliverable_content=auth_content,
        verdict_type="APPROVED",
        create_test_file=True,
    )
    # Add pyproject.toml declaring flask
    (project / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nversion = "1.0.0"\n'
        'dependencies = ["flask"]\n',
        encoding="utf-8",
    )
    gate = D024Gate()

    decision = gate.evaluate_work_order(project, "WO-102")
    assert decision.passed is True
    assert decision.verdict_status == "APPROVED"


# ─── 9. Part B: Architecture Dependency Declaration & Scaffolding Tests ───


def test_is_manifest_permitted_by_contract_evaluation():
    """Verify is_manifest_permitted_by_contract correctly evaluates AgentContract and dict contracts."""
    from validators.kernel.contract import AgentContract

    # Direct booleans
    assert is_manifest_permitted_by_contract(True) is True
    assert is_manifest_permitted_by_contract(False) is False
    assert is_manifest_permitted_by_contract(None) is False

    # AgentContract instances
    scaffolding_contract = AgentContract(
        agent_id="codex",
        work_order="WO-001",
        allow=("requirements.txt",),
        deny=(".git/**",),
        write_mode="read-write",
    )
    assert is_manifest_permitted_by_contract(scaffolding_contract) is True

    pyproject_contract = AgentContract(
        agent_id="codex",
        work_order="WO-001",
        allow=("pyproject.toml",),
        deny=(),
        write_mode="read-write",
    )
    assert is_manifest_permitted_by_contract(pyproject_contract) is True

    worker_code_contract = AgentContract(
        agent_id="codex",
        work_order="WO-002",
        allow=("src/**",),
        deny=(".git/**",),
        write_mode="read-write",
    )
    assert is_manifest_permitted_by_contract(worker_code_contract) is False

    readonly_manifest_contract = AgentContract(
        agent_id="codex",
        work_order="WO-001",
        allow=("requirements.txt",),
        deny=(),
        write_mode="read-only",
    )
    assert is_manifest_permitted_by_contract(readonly_manifest_contract) is False

    denied_manifest_contract = AgentContract(
        agent_id="codex",
        work_order="WO-001",
        allow=("*",),
        deny=(
            "requirements.txt",
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "requirements*.txt",
            "*-requirements.txt",
            "requirements.in",
            "requirements-dev.txt",
            "dev-requirements.txt",
        ),
        write_mode="read-write",
    )
    assert is_manifest_permitted_by_contract(denied_manifest_contract) is False

    # Raw YAML dict contracts
    scaffolding_dict = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "requirements.txt"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
    }
    assert is_manifest_permitted_by_contract(scaffolding_dict) is True

    worker_dict = {
        "agent_id": "codex",
        "work_order": "WO-002",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
    }
    assert is_manifest_permitted_by_contract(worker_dict) is False

    worker_readonly_dict = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "requirements.txt"}],
            "deny": [],
            "write": "read-only",
        },
    }
    assert is_manifest_permitted_by_contract(worker_readonly_dict) is False


def test_scope_aware_diagnostics_missing_manifest(tmp_path: Path):
    """Dependency-gate diagnostic distinguishes permitted vs outside scope when manifest is missing."""
    project = tmp_path / "project"
    project.mkdir()
    deliv = project / "src" / "api" / "auth.py"
    deliv.parent.mkdir(parents=True)
    deliv.write_text("import requests\nimport flask\n", encoding="utf-8")

    # 1. Permitted: Worker contract allows manifest writing
    permitted_contract = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "requirements.txt"}, {"module": "src/**"}],
            "deny": [],
            "write": "read-write",
        },
    }
    res_permitted = check_import_satisfiability(deliv, project_root=project, contract=permitted_contract)
    assert res_permitted.passed is False
    assert res_permitted.manifest_permitted is True
    assert "Your contract permits writing dependency manifests." in str(res_permitted.diagnostic)
    assert "Use write_file to create requirements.txt or pyproject.toml" in str(res_permitted.diagnostic)

    # 2. Outside Scope: Worker contract confines worker to code deliverables
    confined_contract = {
        "agent_id": "codex",
        "work_order": "WO-002",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [],
            "write": "read-write",
        },
    }
    res_confined = check_import_satisfiability(deliv, project_root=project, contract=confined_contract)
    assert res_confined.passed is False
    assert res_confined.manifest_permitted is False
    assert "Dependency manifest creation is OUTSIDE your assigned contract scope." in str(res_confined.diagnostic)
    assert "Architecture (Claude) must provision dependencies via an explicit scaffolding Work Order" in str(res_confined.diagnostic)

    # 3. Neutral: No contract passed
    res_neutral = check_import_satisfiability(deliv, project_root=project, contract=None)
    assert res_neutral.passed is False
    assert res_neutral.manifest_permitted is None
    assert "Create a dependency manifest or remove undeclared external imports." in str(res_neutral.diagnostic)


def test_scope_aware_diagnostics_undeclared_imports_existing_manifest(tmp_path: Path):
    """Dependency-gate diagnostic distinguishes permitted vs outside scope when imports are undeclared."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    deliv = project / "src" / "api" / "db.py"
    deliv.parent.mkdir(parents=True)
    deliv.write_text("import flask\nimport sqlalchemy\n", encoding="utf-8")

    # 1. Permitted: Worker contract allows modifying manifest
    permitted_contract = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "requirements.txt"}, {"module": "src/**"}],
            "deny": [],
            "write": "read-write",
        },
    }
    res_permitted = check_import_satisfiability(deliv, project_root=project, contract=permitted_contract)
    assert res_permitted.passed is False
    assert res_permitted.manifest_permitted is True
    assert res_permitted.undeclared_imports == ("sqlalchemy",)
    assert "Your contract permits updating dependency manifests." in str(res_permitted.diagnostic)
    assert "Declare them in pyproject.toml or requirements.txt using write_file." in str(res_permitted.diagnostic)

    # 2. Outside Scope: Worker contract cannot edit manifest
    confined_contract = {
        "agent_id": "codex",
        "work_order": "WO-002",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [],
            "write": "read-write",
        },
    }
    res_confined = check_import_satisfiability(deliv, project_root=project, contract=confined_contract)
    assert res_confined.passed is False
    assert res_confined.manifest_permitted is False
    assert "Modifying the dependency manifest is OUTSIDE your assigned contract scope." in str(res_confined.diagnostic)
    assert "Architecture (Claude) must declare these dependencies via a scaffolding Work Order" in str(res_confined.diagnostic)

    # 3. Neutral: No contract passed
    res_neutral = check_import_satisfiability(deliv, project_root=project, contract=None)
    assert res_neutral.passed is False
    assert res_neutral.manifest_permitted is None
    assert "Declare them in pyproject.toml or requirements.txt, or replace with local/standard library alternatives." in str(res_neutral.diagnostic)


def test_architecture_declaring_dependencies_and_scaffolding_work_order(tmp_path: Path):
    """Architecture explicitly decides stack and dependencies in PLAN.md and generates scaffolding WO."""
    from validators.harness.authoring_gate import AuthoringGate
    from validators.harness.plan import validate_plan_structure
    import yaml

    plan_content = (
        "# Project Plan: Web Portal\n\n"
        "## Current Architecture\n"
        "Web API service requiring Flask web framework, Werkzeug utilities, and Pydantic data models.\n"
        "Stack & Dependencies: Python 3.11, Flask==3.0.0, Werkzeug==3.0.0, Pydantic>=2.0.0.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Environment & Dependency Scaffolding\n"
        "  - [ ] Task 1.1: Author requirements.txt with declared project dependencies (WO-001)\n"
        "- [ ] Milestone 2: Authentication Service\n"
        "  - [ ] Task 2.1: Implement login endpoint consuming Flask (WO-002)\n"
    )
    is_valid, errors = validate_plan_structure(plan_content)
    assert is_valid is True, errors

    # Scaffolding Work Order
    scaffolding_wo = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Configure Project Dependencies",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {
            "type": "config",
            "path": "requirements.txt",
            "description": "Project dependency manifest declaring required packages",
        },
        "description": "Create requirements.txt declaring Flask, Werkzeug, and Pydantic.",
    }
    scaffolding_contract = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-001",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "requirements.txt"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 30000},
    }

    # Implementation Work Order dependent on scaffolding
    implementation_wo = {
        "id": "WO-002",
        "type": "FEATURE",
        "title": "Implement Login API Endpoint",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": ["WO-001"],
        "deliverable": {
            "type": "code",
            "path": "src/api/auth.py",
            "description": "Authentication endpoint handler",
        },
        "description": "Implement authentication endpoint handler importing Flask.",
    }
    implementation_contract = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-002",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 10, "max_tokens": 30000},
    }

    gate = AuthoringGate()
    # Validate artifacts
    res_wo1 = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", yaml.dump(scaffolding_wo), agent="claude")
    assert res_wo1.passed is True, res_wo1.errors

    res_c1 = gate.validate_artifact_content(".sync/contracts/WO-001.yaml", yaml.dump(scaffolding_contract), agent="claude")
    assert res_c1.passed is True, res_c1.errors

    res_wo2 = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", yaml.dump(implementation_wo), agent="claude")
    assert res_wo2.passed is True, res_wo2.errors

    res_c2 = gate.validate_artifact_content(".sync/contracts/WO-002.yaml", yaml.dump(implementation_contract), agent="claude")
    assert res_c2.passed is True, res_c2.errors


def test_scaffolding_work_order_creates_manifest_and_dependent_worker_consumes_it(tmp_path: Path):
    """Scaffolding worker creates manifest, and dependent worker consumes it successfully."""
    project = tmp_path / "project"
    project.mkdir()

    # Step 1: Scaffolding worker creates requirements.txt
    req_file = project / "requirements.txt"
    req_file.write_text("flask==3.0.0\nwerkzeug>=3.0.0\npydantic>=2.0.0\n", encoding="utf-8")

    scaffolding_contract = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [{"module": "requirements.txt"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
    }
    assert is_manifest_permitted_by_contract(scaffolding_contract) is True

    # Verify manifest was read properly
    declared, manifest_found = read_project_dependencies(project)
    assert manifest_found is True
    assert "flask" in declared
    assert "werkzeug" in declared
    assert "pydantic" in declared

    # Step 2: Dependent worker executes implementation WO-002
    auth_file = project / "src" / "api" / "auth.py"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(
        "from flask import Flask, request, jsonify\n"
        "from werkzeug.security import generate_password_hash\n"
        "from pydantic import BaseModel\n\n"
        "class LoginModel(BaseModel):\n"
        "    username: str\n"
        "    password: str\n\n"
        "app = Flask(__name__)\n",
        encoding="utf-8",
    )

    implementation_contract = {
        "agent_id": "codex",
        "work_order": "WO-002",
        "scope": {
            "allow": [{"module": "src/**"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
    }
    assert is_manifest_permitted_by_contract(implementation_contract) is False

    sat_result = check_import_satisfiability(
        auth_file,
        project_root=project,
        contract=implementation_contract,
    )
    assert sat_result.passed is True
    assert sat_result.undeclared_imports == ()
    assert sat_result.manifest_found is True
    assert set(sat_result.external_imports) == {"flask", "werkzeug", "pydantic"}


def test_worker_denied_manifest_access_when_outside_contract_scope(tmp_path: Path):
    """Worker whose contract scope is confined to src/** is denied manifest writes by ToolGateway."""
    from validators.kernel.contract import AgentContract
    from validators.kernel.identity import AuthorizationPolicy
    from validators.kernel.boundary import RuntimeBoundary
    from validators.kernel.operations import OperationJournal
    from validators.kernel.workspace import ScratchWorkspace
    from validators.kernel.tools import ToolGateway

    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    # Implementation contract confined to src/**
    contract = AgentContract(
        agent_id="codex",
        work_order="WO-002",
        allow=("workspace/src/**", "src/**"),
        deny=(".git/**",),
        write_mode="read-write",
    )

    workspace = ScratchWorkspace.create(project, "attempt-1")
    policy = AuthorizationPolicy.permit("policy-1", ["write_file", "read_file"])
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal=journal)
    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-1",
        attempt_id="attempt-1",
        actor_id="codex",
        provider_id="provider-1",
    )

    # Worker can write within allowed scope
    gateway.write_file("src/api/auth.py", "# auth code\n")
    assert (workspace.root / "src" / "api" / "auth.py").exists()

    # Worker CANNOT write or tamper with requirements.txt
    with pytest.raises(PermissionError) as exc_info:
        gateway.write_file("requirements.txt", "malicious-pkg==1.0.0\n")
    assert "outside allowed scope" in str(exc_info.value) or "denied" in str(exc_info.value)

