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
