"""Dependency & Import Satisfiability Gate.

Mechanically verifies that Python deliverables modified or authored by agents
do not import undeclared external third-party packages.

Enforces:
1. AST extraction of top-level imported module names.
2. Standard library module exclusion (sys.stdlib_module_names / builtins).
3. Project-local module and package exclusion.
4. Dependency manifest parsing (pyproject.toml, requirements*.txt).
5. Explicit import-name to distribution-name mapping (e.g. yaml -> pyyaml).
6. Fail-closed verification when external imports exist without manifests.
7. Clear diagnostics reporting offending files and undeclared imports.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

# PEP 508 / Package name pattern from compiler
REQ_PKG_PATTERN = re.compile(r"^([a-zA-Z0-9_\-\.]+)\s*([<>=!~;@\s].*)?$")

# Comprehensive fallback standard library modules
FALLBACK_STDLIB_MODULES: frozenset[str] = frozenset({
    "__future__",
    "_thread",
    "abc",
    "aifc",
    "argparse",
    "array",
    "ast",
    "asynchat",
    "asyncio",
    "asyncore",
    "atexit",
    "audioop",
    "base64",
    "bdb",
    "binascii",
    "bisect",
    "builtins",
    "bz2",
    "cProfile",
    "calendar",
    "cgi",
    "cgitb",
    "chunk",
    "cmath",
    "cmd",
    "code",
    "codecs",
    "codeop",
    "collections",
    "colorsys",
    "compileall",
    "concurrent",
    "configparser",
    "contextlib",
    "contextvars",
    "copy",
    "copyreg",
    "crypt",
    "csv",
    "ctypes",
    "curses",
    "dataclasses",
    "datetime",
    "dbm",
    "decimal",
    "difflib",
    "dis",
    "distutils",
    "doctest",
    "email",
    "encodings",
    "ensurepip",
    "enum",
    "errno",
    "faulthandler",
    "fcntl",
    "filecmp",
    "fileinput",
    "fnmatch",
    "fractions",
    "ftplib",
    "functools",
    "gc",
    "getopt",
    "getpass",
    "gettext",
    "glob",
    "graphlib",
    "grp",
    "gzip",
    "hashlib",
    "heapq",
    "hmac",
    "html",
    "http",
    "imaplib",
    "imghdr",
    "imp",
    "importlib",
    "inspect",
    "io",
    "ipaddress",
    "itertools",
    "json",
    "keyword",
    "linecache",
    "locale",
    "logging",
    "lzma",
    "mailbox",
    "mailcap",
    "marshal",
    "math",
    "mimetypes",
    "mmap",
    "modulefinder",
    "msilib",
    "msvcrt",
    "multiprocessing",
    "netrc",
    "nis",
    "nntplib",
    "numbers",
    "operator",
    "optparse",
    "os",
    "ossaudiodev",
    "parser",
    "pathlib",
    "pdb",
    "pickle",
    "pickletools",
    "pipes",
    "pkgutil",
    "platform",
    "plistlib",
    "poplib",
    "posix",
    "posixpath",
    "pprint",
    "profile",
    "pstats",
    "pty",
    "pwd",
    "py_compile",
    "pyclbr",
    "pydoc",
    "queue",
    "quopri",
    "random",
    "re",
    "readline",
    "reprlib",
    "resource",
    "rlcompleter",
    "runpy",
    "sched",
    "secrets",
    "select",
    "selectors",
    "shelve",
    "shlex",
    "shutil",
    "signal",
    "site",
    "smtpd",
    "smtplib",
    "sndhdr",
    "socket",
    "socketserver",
    "spwd",
    "sqlite3",
    "sre_compile",
    "sre_constants",
    "sre_parse",
    "ssl",
    "stat",
    "statistics",
    "string",
    "stringprep",
    "struct",
    "subprocess",
    "sunau",
    "symtable",
    "sys",
    "sysconfig",
    "syslog",
    "tabnanny",
    "tarfile",
    "telnetlib",
    "tempfile",
    "termios",
    "test",
    "textwrap",
    "time",
    "timeit",
    "tkinter",
    "token",
    "tokenize",
    "tomllib",
    "trace",
    "traceback",
    "tracemalloc",
    "tty",
    "turtle",
    "turtledemo",
    "types",
    "typing",
    "unicodedata",
    "unittest",
    "urllib",
    "uu",
    "uuid",
    "venv",
    "warnings",
    "wave",
    "weakref",
    "webbrowser",
    "winreg",
    "winsound",
    "wsgiref",
    "xdrlib",
    "xml",
    "xmlrpc",
    "zipapp",
    "zipfile",
    "zipimport",
    "zlib",
    "zoneinfo",
})

# Explicit mapping from Python import name to set of known distribution package names.
# Normalized according to PEP 503 (lowercase, dashes instead of underscores).
KNOWN_IMPORT_TO_DISTRIBUTIONS: dict[str, set[str]] = {
    "yaml": {"pyyaml"},
    "bs4": {"beautifulsoup4"},
    "cv2": {"opencv-python", "opencv-python-headless", "opencv-contrib-python"},
    "dotenv": {"python-dotenv"},
    "dateutil": {"python-dateutil"},
    "pil": {"pillow"},
    "jwt": {"pyjwt"},
    "sklearn": {"scikit-learn"},
    "openssl": {"pyopenssl"},
    "serial": {"pyserial"},
    "websocket": {"websocket-client"},
    "psycopg2": {"psycopg2", "psycopg2-binary"},
    "magic": {"python-magic", "python-magic-bin"},
    "jose": {"python-jose"},
    "git": {"gitpython"},
    "multipart": {"python-multipart"},
    "bio": {"biopython"},
    "pydantic_core": {"pydantic", "pydantic-core"},
    "fitz": {"pymupdf"},
    "docx": {"python-docx"},
    "pptx": {"python-pptx"},
    "google": {
        "google-api-python-client",
        "protobuf",
        "google-auth",
        "google-cloud-storage",
        "google-cloud-core",
    },
    "grpc": {"grpcio"},
    "crypto": {"pycryptodome", "pycrypto"},
    "cryptography": {"cryptography"},
    "sqlalchemy": {"sqlalchemy"},
    "tornado": {"tornado"},
    "redis": {"redis"},
    "celery": {"celery"},
    "boto3": {"boto3"},
    "botocore": {"botocore"},
    "aiohttp": {"aiohttp"},
    "httpx": {"httpx"},
    "requests": {"requests"},
    "flask": {"flask"},
    "werkzeug": {"werkzeug"},
    "jinja2": {"jinja2"},
    "click": {"click"},
    "fastapi": {"fastapi"},
    "starlette": {"starlette"},
    "pydantic": {"pydantic"},
    "uvicorn": {"uvicorn"},
    "pytest": {"pytest"},
}

EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    ".sync",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "egg-info",
})


@dataclass(frozen=True)
class ImportSatisfiabilityResult:
    """Outcome of examining imports in a Python deliverable against declared dependencies."""

    passed: bool
    file_path: str
    all_imports: tuple[str, ...]
    external_imports: tuple[str, ...]
    undeclared_imports: tuple[str, ...]
    manifest_found: bool
    declared_dependencies: tuple[str, ...]
    diagnostic: str | None = None
    manifest_permitted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "file_path": self.file_path,
            "all_imports": list(self.all_imports),
            "external_imports": list(self.external_imports),
            "undeclared_imports": list(self.undeclared_imports),
            "manifest_found": self.manifest_found,
            "declared_dependencies": list(self.declared_dependencies),
            "diagnostic": self.diagnostic,
            "manifest_permitted": self.manifest_permitted,
        }


def normalize_distribution_name(name: str) -> str:
    """Normalize a package distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def extract_package_name_from_spec(spec: str) -> str | None:
    """Extract bare package name from a PEP 508 requirement specification.

    Handles version specifiers, extras (e.g. requests[security]), URLs (@),
    and environment markers (;).
    """
    cleaned = spec.strip().strip('"').strip("'")
    if not cleaned or cleaned.startswith(("#", "-", "/", "\\")):
        return None

    # Strip inline comments
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0].strip()

    # Strip environment markers
    if ";" in cleaned:
        cleaned = cleaned.split(";", 1)[0].strip()

    # Strip direct URL / VCS references
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()

    match = re.match(r"^([a-zA-Z0-9_\-\.]+)", cleaned)
    if match:
        pkg = match.group(1).strip()
        if pkg and not pkg.startswith(("-", "/")):
            return pkg
    return None


def extract_top_level_imports(source_code: str) -> set[str]:
    """Parse Python source code and extract all top-level imported module names using AST.

    Ignores relative imports (e.g. 'from . import foo') and __future__.
    """
    try:
        tree = ast.parse(source_code)
    except (SyntaxError, ValueError):
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0].strip()
                if top and top != "__future__":
                    imports.add(top)
        elif isinstance(node, ast.ImportFrom):
            # node.level > 0 represents a relative import
            if node.level and node.level > 0:
                continue
            if node.module:
                top = node.module.split(".", 1)[0].strip()
                if top and top != "__future__":
                    imports.add(top)

    return imports


def is_standard_library(module_name: str) -> bool:
    """Determine whether module_name belongs to Python's standard library or builtins."""
    if not module_name:
        return False
    if module_name == "__future__":
        return True
    if module_name in sys.builtin_module_names:
        return True
    if hasattr(sys, "stdlib_module_names") and module_name in sys.stdlib_module_names:
        return True
    return module_name.lower() in FALLBACK_STDLIB_MODULES


def is_local_module(
    module_name: str,
    project_root: Path,
    current_file_path: Path | None = None,
    staged_root: Path | None = None,
) -> bool:
    """Determine whether module_name is demonstrably local to the project.

    Checks:
    - Root-level modules (<root>/<module>.py or <root>/<module>/)
    - Source directories: <root>/src, <root>/lib, <root>/app
    - Sibling modules to current_file_path
    - Parent directories within the project
    - Both authoritative project_root and staging root if provided
    """
    if not module_name:
        return False

    roots = [project_root]
    if staged_root and staged_root.exists() and staged_root.resolve() != project_root.resolve():
        roots.append(staged_root)

    for base in roots:
        search_dirs: list[Path] = [
            base,
            base / "src",
            base / "lib",
            base / "app",
        ]

        if current_file_path:
            # Check sibling directory
            search_dirs.append(current_file_path.parent)
            try:
                rel = current_file_path.relative_to(project_root)
                search_dirs.append(base / rel.parent)
                if staged_root:
                    search_dirs.append(staged_root / rel.parent)
            except ValueError:
                pass

        for d in search_dirs:
            if not d.is_dir():
                continue

            # Direct python file: <dir>/<module_name>.py
            if (d / f"{module_name}.py").is_file():
                return True

            # Package directory: <dir>/<module_name>/
            pkg_dir = d / module_name
            if pkg_dir.is_dir():
                if module_name in ("src", "lib", "app"):
                    return True
                # Regular package with __init__.py or namespace package containing .py
                if (pkg_dir / "__init__.py").is_file():
                    return True
                if any(pkg_dir.rglob("*.py")):
                    return True

    return False


def parse_pyproject_dependencies(content: str) -> set[str]:
    """Parse dependencies from pyproject.toml content."""
    deps: set[str] = set()
    data: dict[str, Any] | None = None

    if tomllib is not None:
        try:
            data = tomllib.loads(content)
        except Exception:
            data = None

    if data is not None:
        # Standard PEP 621 dependencies
        project_sec = data.get("project", {})
        for spec in project_sec.get("dependencies", []):
            if isinstance(spec, str):
                name = extract_package_name_from_spec(spec)
                if name:
                    deps.add(normalize_distribution_name(name))

        # Standard PEP 621 optional-dependencies
        for opt_list in project_sec.get("optional-dependencies", {}).values():
            if isinstance(opt_list, list):
                for spec in opt_list:
                    if isinstance(spec, str):
                        name = extract_package_name_from_spec(spec)
                        if name:
                            deps.add(normalize_distribution_name(name))

        # PEP 735 dependency-groups
        for grp_list in data.get("dependency-groups", {}).values():
            if isinstance(grp_list, list):
                for spec in grp_list:
                    if isinstance(spec, str):
                        name = extract_package_name_from_spec(spec)
                        if name:
                            deps.add(normalize_distribution_name(name))

        # Poetry tool dependencies
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(poetry_deps, dict):
            for pkg in poetry_deps:
                if isinstance(pkg, str) and pkg.lower() != "python":
                    deps.add(normalize_distribution_name(pkg))

        # Poetry group dependencies
        poetry_groups = data.get("tool", {}).get("poetry", {}).get("group", {})
        if isinstance(poetry_groups, dict):
            for grp_data in poetry_groups.values():
                if isinstance(grp_data, dict):
                    grp_deps = grp_data.get("dependencies", {})
                    if isinstance(grp_deps, dict):
                        for pkg in grp_deps:
                            if isinstance(pkg, str) and pkg.lower() != "python":
                                deps.add(normalize_distribution_name(pkg))

        # Build-system requirements
        build_requires = data.get("build-system", {}).get("requires", [])
        if isinstance(build_requires, list):
            for spec in build_requires:
                if isinstance(spec, str):
                    name = extract_package_name_from_spec(spec)
                    if name:
                        deps.add(normalize_distribution_name(name))

    # Regex fallback if tomllib is unavailable or yielded no deps
    if not deps:
        for line in content.splitlines():
            clean = line.strip().strip('"').strip("'").strip(",")
            if clean and not clean.startswith(("[", "#")):
                name = extract_package_name_from_spec(clean)
                if name and name not in {"project", "dependencies", "build-system", "tool"}:
                    deps.add(normalize_distribution_name(name))

    return deps


def parse_requirements_dependencies(content: str) -> set[str]:
    """Parse dependencies from requirements.txt content."""
    deps: set[str] = set()
    for line in content.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("#", "-")):
            continue
        name = extract_package_name_from_spec(clean)
        if name:
            deps.add(normalize_distribution_name(name))
    return deps


def parse_setup_cfg_dependencies(content: str) -> set[str]:
    """Parse install_requires and extras from setup.cfg content."""
    deps: set[str] = set()
    in_install_requires = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_install_requires = stripped in ("[options]", "[options.extras_require]")
            continue
        if in_install_requires and stripped and not stripped.startswith("#"):
            if "=" in stripped and not stripped.startswith("install_requires"):
                continue
            spec = stripped.split("=", 1)[-1].strip() if "=" in stripped else stripped
            name = extract_package_name_from_spec(spec)
            if name:
                deps.add(normalize_distribution_name(name))
    return deps


def read_project_dependencies(
    project_root: Path,
    staged_root: Path | None = None,
) -> tuple[set[str], bool]:
    """Read declared dependencies from project manifests (pyproject.toml, requirements*.txt, setup.cfg).

    Returns (declared_packages, manifest_found).
    """
    declared: set[str] = set()
    manifest_found = False

    roots = [project_root]
    if staged_root and staged_root.exists() and staged_root.resolve() != project_root.resolve():
        roots.insert(0, staged_root)

    checked_manifest_names: set[str] = set()

    for root in roots:
        # 1. pyproject.toml
        pyproject_file = root / "pyproject.toml"
        if pyproject_file.is_file() and "pyproject.toml" not in checked_manifest_names:
            manifest_found = True
            checked_manifest_names.add("pyproject.toml")
            try:
                content = pyproject_file.read_text(encoding="utf-8", errors="replace")
                declared.update(parse_pyproject_dependencies(content))
            except OSError:
                pass

        # 2. requirements*.txt (root and subdirectories excluding .venv, .git, etc.)
        candidate_req_files: list[Path] = []
        for pattern in ("requirements*.txt", "*-requirements.txt", "requirements/*.txt"):
            for p in root.glob(pattern):
                if p.is_file() and not any(part in EXCLUDED_DIR_NAMES for part in p.parts):
                    candidate_req_files.append(p)

        for req_file in sorted(candidate_req_files):
            rel_name = req_file.name
            if rel_name not in checked_manifest_names:
                manifest_found = True
                checked_manifest_names.add(rel_name)
                try:
                    content = req_file.read_text(encoding="utf-8", errors="replace")
                    declared.update(parse_requirements_dependencies(content))
                except OSError:
                    pass

        # 3. setup.cfg
        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file() and "setup.cfg" not in checked_manifest_names:
            manifest_found = True
            checked_manifest_names.add("setup.cfg")
            try:
                content = setup_cfg.read_text(encoding="utf-8", errors="replace")
                declared.update(parse_setup_cfg_dependencies(content))
            except OSError:
                pass

    return declared, manifest_found


def is_import_declared(
    import_name: str,
    declared_distributions: set[str],
    custom_mapping: dict[str, set[str]] | None = None,
) -> bool:
    """Check whether import_name is satisfied by the declared distribution names."""
    canonical_import = normalize_distribution_name(import_name)

    # Direct match (e.g. import 'flask' -> declared 'flask')
    if canonical_import in declared_distributions:
        return True

    # Check custom mappings if supplied
    if custom_mapping:
        mapped = custom_mapping.get(canonical_import) or custom_mapping.get(import_name.lower())
        if mapped:
            for dist in mapped:
                if normalize_distribution_name(dist) in declared_distributions:
                    return True

    # Check known conservative mappings (e.g. 'yaml' -> 'pyyaml', 'bs4' -> 'beautifulsoup4')
    mapped_dists = (
        KNOWN_IMPORT_TO_DISTRIBUTIONS.get(canonical_import)
        or KNOWN_IMPORT_TO_DISTRIBUTIONS.get(import_name.lower())
    )
    if mapped_dists:
        for dist in mapped_dists:
            if normalize_distribution_name(dist) in declared_distributions:
                return True

    return False


MANIFEST_CANDIDATE_TARGETS: tuple[str, ...] = (
    "requirements.txt",
    "requirements-dev.txt",
    "dev-requirements.txt",
    "requirements.in",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
)


def is_manifest_permitted_by_contract(contract: Any) -> bool:
    """Determine whether an agent contract authorizes creating or modifying dependency manifests.

    Supports:
    - bool (direct override)
    - dict (raw contract parsed from .sync/contracts/WO-xxx.yaml)
    - AgentContract instances (validators.kernel.contract.AgentContract)
    - None (returns False)
    """
    if isinstance(contract, bool):
        return contract
    if contract is None:
        return False

    write_mode = "read-only"
    raw_allow: Any = []
    raw_deny: Any = []

    if hasattr(contract, "write_mode") and hasattr(contract, "allow") and hasattr(contract, "deny"):
        write_mode = getattr(contract, "write_mode", "read-only")
        raw_allow = getattr(contract, "allow", ())
        raw_deny = getattr(contract, "deny", ())
    elif isinstance(contract, dict):
        raw_scope = contract.get("scope")
        if isinstance(raw_scope, dict):
            write_mode = raw_scope.get("write", contract.get("write_mode", "read-only"))
            raw_allow = raw_scope.get("allow", [])
            raw_deny = raw_scope.get("deny", [])
        else:
            write_mode = contract.get("write_mode", "read-only")
            raw_allow = contract.get("allow", [])
            raw_deny = contract.get("deny", [])
    else:
        return False

    if write_mode != "read-write":
        return False

    def _extract_rule_strings(rules: Any) -> list[str]:
        result: list[str] = []
        if isinstance(rules, (str, dict)):
            rules = [rules]
        if isinstance(rules, (list, tuple)):
            for r in rules:
                if isinstance(r, dict):
                    target = r.get("module") or r.get("target") or r.get("path")
                    if target and isinstance(target, str):
                        result.append(target.strip().replace("\\", "/"))
                elif isinstance(r, str) and r.strip():
                    result.append(r.strip().replace("\\", "/"))
        return result

    allow_list = _extract_rule_strings(raw_allow)
    deny_list = _extract_rule_strings(raw_deny)

    if not allow_list:
        return False

    def _matches_rule(target: str, rule: str) -> bool:
        norm_target = target.replace("\\", "/").strip().lstrip("/")
        norm_rule = rule.replace("\\", "/").strip().lstrip("/")
        if norm_rule.startswith("./"):
            norm_rule = norm_rule[2:]
        if norm_target.startswith("./"):
            norm_target = norm_target[2:]

        if norm_rule in ("*", "**"):
            return True
        if fnmatch.fnmatch(norm_target, norm_rule):
            return True
        if norm_target == norm_rule:
            return True
        if norm_target.startswith(norm_rule.rstrip("/") + "/"):
            return True
        if norm_rule.startswith("workspace/"):
            sub_rule = norm_rule[len("workspace/") :]
            if (
                fnmatch.fnmatch(norm_target, sub_rule)
                or norm_target == sub_rule
                or norm_target.startswith(sub_rule.rstrip("/") + "/")
            ):
                return True
        return False

    for candidate in MANIFEST_CANDIDATE_TARGETS:
        denied = any(_matches_rule(candidate, d) for d in deny_list)
        if denied:
            continue
        allowed = any(_matches_rule(candidate, a) for a in allow_list)
        if allowed:
            return True

    return False


def check_import_satisfiability(
    target_file: Path,
    project_root: Path,
    staged_root: Path | None = None,
    source_code: str | None = None,
    custom_mapping: dict[str, set[str]] | None = None,
    contract: Any = None,
    manifest_permitted: bool | None = None,
) -> ImportSatisfiabilityResult:
    """Evaluate whether all external imports in target_file are declared in the project's dependencies.

    Fail-closed:
    - If external imports exist but no manifest exists -> fails closed.
    - If external imports exist that are not declared in manifests -> fails closed.
    - If file only imports standard library or local modules -> passes.

    Scope-aware diagnostics:
    - Distinguishes whether dependency manifest authoring is permitted by the worker contract
      or outside assigned contract scope (requiring Architecture scaffolding).
    """
    if manifest_permitted is None and contract is not None:
        manifest_permitted = is_manifest_permitted_by_contract(contract)

    try:
        rel_path = target_file.relative_to(project_root).as_posix()
    except ValueError:
        try:
            rel_path = target_file.relative_to(staged_root).as_posix() if staged_root else target_file.as_posix()
        except ValueError:
            rel_path = target_file.as_posix()

    if source_code is None:
        try:
            source_code = target_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ImportSatisfiabilityResult(
                passed=False,
                file_path=rel_path,
                all_imports=(),
                external_imports=(),
                undeclared_imports=(),
                manifest_found=False,
                declared_dependencies=(),
                diagnostic=f"Cannot read deliverable '{rel_path}': {e}",
                manifest_permitted=manifest_permitted,
            )

    all_imports = extract_top_level_imports(source_code)
    external_imports: set[str] = set()

    for mod in all_imports:
        if is_standard_library(mod):
            continue
        if is_local_module(
            mod,
            project_root=project_root,
            current_file_path=target_file,
            staged_root=staged_root,
        ):
            continue
        external_imports.add(mod)

    declared_deps, manifest_found = read_project_dependencies(project_root, staged_root=staged_root)

    # Case 1: No external imports (stdlib and/or local only) -> always passes
    if not external_imports:
        return ImportSatisfiabilityResult(
            passed=True,
            file_path=rel_path,
            all_imports=tuple(sorted(all_imports)),
            external_imports=(),
            undeclared_imports=(),
            manifest_found=manifest_found,
            declared_dependencies=tuple(sorted(declared_deps)),
            diagnostic=None,
            manifest_permitted=manifest_permitted,
        )

    # Case 2: External imports exist, but NO manifest exists -> fail closed
    if not manifest_found:
        sorted_ext = sorted(external_imports)
        if manifest_permitted is True:
            diagnostic = (
                f"Deliverable '{rel_path}' imports external module(s) {sorted_ext} "
                f"but no dependency manifest (pyproject.toml, requirements*.txt) was found in project. "
                f"Your contract permits writing dependency manifests. "
                f"Use write_file to create requirements.txt or pyproject.toml declaring these dependencies."
            )
        elif manifest_permitted is False:
            diagnostic = (
                f"Deliverable '{rel_path}' imports external module(s) {sorted_ext} "
                f"but no dependency manifest (pyproject.toml, requirements*.txt) was found in project. "
                f"Dependency manifest creation is OUTSIDE your assigned contract scope. "
                f"Architecture (Claude) must provision dependencies via an explicit scaffolding Work Order before implementation."
            )
        else:
            diagnostic = (
                f"Deliverable '{rel_path}' imports external module(s) {sorted_ext} "
                f"but no dependency manifest (pyproject.toml, requirements*.txt) was found in project. "
                f"Create a dependency manifest or remove undeclared external imports."
            )
        return ImportSatisfiabilityResult(
            passed=False,
            file_path=rel_path,
            all_imports=tuple(sorted(all_imports)),
            external_imports=tuple(sorted_ext),
            undeclared_imports=tuple(sorted_ext),
            manifest_found=False,
            declared_dependencies=(),
            diagnostic=diagnostic,
            manifest_permitted=manifest_permitted,
        )

    # Case 3: External imports exist, verify against declared dependencies
    undeclared: set[str] = set()
    for mod in external_imports:
        if not is_import_declared(mod, declared_deps, custom_mapping=custom_mapping):
            undeclared.add(mod)

    if undeclared:
        sorted_und = sorted(undeclared)
        if manifest_permitted is True:
            diagnostic = (
                f"Deliverable '{rel_path}' imports undeclared third-party module(s): {', '.join(sorted_und)}. "
                f"Your contract permits updating dependency manifests. "
                f"Declare them in pyproject.toml or requirements.txt using write_file."
            )
        elif manifest_permitted is False:
            diagnostic = (
                f"Deliverable '{rel_path}' imports undeclared third-party module(s): {', '.join(sorted_und)}. "
                f"Modifying the dependency manifest is OUTSIDE your assigned contract scope. "
                f"Architecture (Claude) must declare these dependencies via a scaffolding Work Order, or replace them with local/standard library alternatives."
            )
        else:
            diagnostic = (
                f"Deliverable '{rel_path}' imports undeclared third-party module(s): {', '.join(sorted_und)}. "
                f"Declare them in pyproject.toml or requirements.txt, or replace with local/standard library alternatives."
            )
        return ImportSatisfiabilityResult(
            passed=False,
            file_path=rel_path,
            all_imports=tuple(sorted(all_imports)),
            external_imports=tuple(sorted(external_imports)),
            undeclared_imports=tuple(sorted_und),
            manifest_found=True,
            declared_dependencies=tuple(sorted(declared_deps)),
            diagnostic=diagnostic,
            manifest_permitted=manifest_permitted,
        )

    return ImportSatisfiabilityResult(
        passed=True,
        file_path=rel_path,
        all_imports=tuple(sorted(all_imports)),
        external_imports=tuple(sorted(external_imports)),
        undeclared_imports=(),
        manifest_found=True,
        declared_dependencies=tuple(sorted(declared_deps)),
        diagnostic=None,
        manifest_permitted=manifest_permitted,
    )


def check_multiple_deliverables(
    deliverable_paths: Iterable[Path],
    project_root: Path,
    staged_root: Path | None = None,
    custom_mapping: dict[str, set[str]] | None = None,
    contract: Any = None,
    manifest_permitted: bool | None = None,
) -> tuple[bool, list[ImportSatisfiabilityResult]]:
    """Check import satisfiability across multiple deliverable files."""
    results: list[ImportSatisfiabilityResult] = []
    all_passed = True
    for p in deliverable_paths:
        res = check_import_satisfiability(
            p,
            project_root=project_root,
            staged_root=staged_root,
            custom_mapping=custom_mapping,
            contract=contract,
            manifest_permitted=manifest_permitted,
        )
        results.append(res)
        if not res.passed:
            all_passed = False
    return all_passed, results
