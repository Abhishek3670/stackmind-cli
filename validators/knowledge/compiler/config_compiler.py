"""Configuration compiler frontend augmentation for the knowledge compiler.

Parses dependency files (pyproject.toml, requirements.txt), Docker files (Dockerfile,
docker-compose.yml), and .env files to emit environment mapping IR.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .parse import DEFAULT_EXCLUDED_DIRS, ParsedFile, ParsedRelation, ParsedSymbol

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

EXCLUDED_DIRS = DEFAULT_EXCLUDED_DIRS

REQ_PKG_PATTERN = re.compile(r"^([a-zA-Z0-9_\-\.]+)\s*([<>=!~].*)?$")


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with project configuration symbols and relations."""
    if project_path is None:
        return

    project_path = project_path.resolve()
    parsed_by_path = {p.path: p for p in parsed_files}

    # 1. pyproject.toml
    pyproject_path = project_path / "pyproject.toml"
    if pyproject_path.exists():
        _process_config_file(
            pyproject_path.relative_to(project_path).as_posix(),
            pyproject_path,
            _compile_pyproject,
            parsed_files,
            parsed_by_path,
        )

    # 2. requirements*.txt, Dockerfiles, and compose files
    import fnmatch
    requirements_files = [
        p for p in project_path.rglob("requirements*.txt")
        if not any(part in EXCLUDED_DIRS for part in p.relative_to(project_path).parts)
    ]
    docker_files = [
        p for p in project_path.rglob("Dockerfile*")
        if not any(part in EXCLUDED_DIRS for part in p.relative_to(project_path).parts)
    ]
    compose_files = [
        p for p in project_path.rglob("docker-compose*")
        if (p.name.endswith(".yml") or p.name.endswith(".yaml"))
        and not any(part in EXCLUDED_DIRS for part in p.relative_to(project_path).parts)
    ]

    for req_path in sorted(requirements_files):
        rel = req_path.relative_to(project_path).as_posix()
        _process_config_file(rel, req_path, _compile_requirements, parsed_files, parsed_by_path)

    for docker_path in sorted(docker_files):
        rel = docker_path.relative_to(project_path).as_posix()
        _process_config_file(rel, docker_path, _compile_dockerfile, parsed_files, parsed_by_path)

    for compose_path in sorted(compose_files):
        rel = compose_path.relative_to(project_path).as_posix()
        _process_config_file(rel, compose_path, _compile_docker_compose, parsed_files, parsed_by_path)

    # 4. .env* files (.env.example, .env.defaults, .env.template, .env)
    for env_path in sorted(project_path.glob(".env*")):
        if env_path.is_file():
            rel = env_path.relative_to(project_path).as_posix()
            _process_config_file(rel, env_path, _compile_env_file, parsed_files, parsed_by_path)


def _process_config_file(
    rel_path: str,
    full_path: Path,
    compiler_fn: callable,
    parsed_files: list[ParsedFile],
    parsed_by_path: dict[str, ParsedFile],
) -> None:
    try:
        content = full_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return

    symbols, relations = compiler_fn(rel_path, content)
    if not symbols:
        return

    if rel_path in parsed_by_path:
        parsed = parsed_by_path[rel_path]
        parsed.symbols.extend(symbols)
        parsed.relations.extend(relations)
    else:
        parsed_file = ParsedFile(
            path=rel_path,
            module_name=rel_path.replace("/", ".").strip("."),
            tree=None,
            symbols=symbols,
            relations=relations,
        )
        parsed_files.append(parsed_file)
        parsed_by_path[rel_path] = parsed_file


def _compile_pyproject(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    config_symbol = ParsedSymbol(
        kind="ConfigFile",
        path=rel_path,
        qualified_name=f"config:{rel_path}",
        module_name="pyproject",
        signature="config pyproject.toml",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(config_symbol)

    deps: list[str] = []
    if tomllib is not None:
        try:
            data = tomllib.loads(content)
            project_sec = data.get("project", {})
            deps.extend(project_sec.get("dependencies", []))
            for opt_deps in project_sec.get("optional-dependencies", {}).values():
                deps.extend(opt_deps)
        except Exception:
            pass

    if not deps:
        # Regex fallback for dependencies
        for line in lines:
            line_str = line.strip().strip('"').strip("'").strip(",")
            match = REQ_PKG_PATTERN.match(line_str)
            if match and match.group(1) not in {"project", "dependencies", "build-system"}:
                deps.append(line_str)

    for dep_str in deps:
        match = REQ_PKG_PATTERN.match(dep_str.strip())
        if match:
            pkg_name = match.group(1)
            constraint = match.group(2) or ""
            dep_qualname = f"dep:{pkg_name}"

            symbols.append(
                ParsedSymbol(
                    kind="Dependency",
                    path=rel_path,
                    qualified_name=dep_qualname,
                    module_name="pyproject",
                    signature=f"dependency {pkg_name} {constraint}".strip(),
                    location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                    content_hash=hashlib.sha256(dep_str.encode("utf-8")).hexdigest(),
                    owner_qualified_name=config_symbol.qualified_name,
                )
            )
            relations.append(
                ParsedRelation(
                    path=rel_path,
                    source_qualified_name=config_symbol.qualified_name,
                    relation="CONFIG_REQUIRES_DEP",
                    target_name=dep_qualname,
                    line=1,
                )
            )

    return symbols, relations


def _compile_requirements(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    config_symbol = ParsedSymbol(
        kind="ConfigFile",
        path=rel_path,
        qualified_name=f"config:{rel_path}",
        module_name=rel_path.replace("/", ".").rstrip(".txt"),
        signature=f"config {rel_path}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(config_symbol)

    for idx, line in enumerate(lines, start=1):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("#") or clean_line.startswith("-"):
            continue
        match = REQ_PKG_PATTERN.match(clean_line)
        if match:
            pkg_name = match.group(1)
            constraint = match.group(2) or ""
            dep_qualname = f"dep:{pkg_name}"

            symbols.append(
                ParsedSymbol(
                    kind="Dependency",
                    path=rel_path,
                    qualified_name=dep_qualname,
                    module_name=rel_path.replace("/", ".").rstrip(".txt"),
                    signature=f"dependency {pkg_name} {constraint}".strip(),
                    location={"line": idx, "column": 0, "end_line": idx, "end_column": len(line)},
                    content_hash=hashlib.sha256(clean_line.encode("utf-8")).hexdigest(),
                    owner_qualified_name=config_symbol.qualified_name,
                )
            )
            relations.append(
                ParsedRelation(
                    path=rel_path,
                    source_qualified_name=config_symbol.qualified_name,
                    relation="CONFIG_REQUIRES_DEP",
                    target_name=dep_qualname,
                    line=idx,
                )
            )

    return symbols, relations


def _compile_dockerfile(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    base_image = "unknown"
    for line in lines:
        if line.strip().upper().startswith("FROM "):
            base_image = line.strip().split(maxsplit=1)[1]
            break

    config_symbol = ParsedSymbol(
        kind="ConfigFile",
        path=rel_path,
        qualified_name=f"config:{rel_path}",
        module_name=rel_path.replace("/", "."),
        signature=f"dockerfile base={base_image}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(config_symbol)

    service_symbol = ParsedSymbol(
        kind="DockerService",
        path=rel_path,
        qualified_name=f"docker:{rel_path}",
        module_name=rel_path.replace("/", "."),
        signature=f"service {rel_path} base={base_image}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=config_symbol.qualified_name,
    )
    symbols.append(service_symbol)

    relations.append(
        ParsedRelation(
            path=rel_path,
            source_qualified_name=config_symbol.qualified_name,
            relation="CONFIG_DEFINES_SERVICE",
            target_name=service_symbol.qualified_name,
            line=1,
        )
    )

    for idx, line in enumerate(lines, start=1):
        clean = line.strip()
        if clean.upper().startswith("ENV "):
            parts = clean.split(maxsplit=2)
            if len(parts) >= 2:
                var_expr = parts[1]
                var_name = var_expr.split("=")[0]
                env_qualname = f"env:{var_name}"

                symbols.append(
                    ParsedSymbol(
                        kind="EnvVar",
                        path=rel_path,
                        qualified_name=env_qualname,
                        module_name=rel_path.replace("/", "."),
                        signature=f"env_var {var_name}",
                        location={"line": idx, "column": 0, "end_line": idx, "end_column": len(line)},
                        content_hash=hashlib.sha256(clean.encode("utf-8")).hexdigest(),
                        owner_qualified_name=service_symbol.qualified_name,
                    )
                )
                relations.append(
                    ParsedRelation(
                        path=rel_path,
                        source_qualified_name=service_symbol.qualified_name,
                        relation="CONFIG_DEFINES_ENV",
                        target_name=env_qualname,
                        line=idx,
                    )
                )

    return symbols, relations


def _compile_docker_compose(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    config_symbol = ParsedSymbol(
        kind="ConfigFile",
        path=rel_path,
        qualified_name=f"config:{rel_path}",
        module_name=rel_path.replace("/", "."),
        signature=f"docker-compose {rel_path}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(config_symbol)

    current_service = None
    in_services = False

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if indent == 0 and stripped == "services:":
            in_services = True
            continue
        elif indent == 0 and in_services:
            in_services = False

        if in_services and indent == 2 and stripped.endswith(":"):
            service_name = stripped.rstrip(":")
            current_service = f"docker:{service_name}"
            symbols.append(
                ParsedSymbol(
                    kind="DockerService",
                    path=rel_path,
                    qualified_name=current_service,
                    module_name=rel_path.replace("/", "."),
                    signature=f"service {service_name}",
                    location={"line": idx, "column": 0, "end_line": idx, "end_column": len(line)},
                    content_hash=hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                    owner_qualified_name=config_symbol.qualified_name,
                )
            )
            relations.append(
                ParsedRelation(
                    path=rel_path,
                    source_qualified_name=config_symbol.qualified_name,
                    relation="CONFIG_DEFINES_SERVICE",
                    target_name=current_service,
                    line=idx,
                )
            )

    return symbols, relations


def _compile_env_file(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    config_symbol = ParsedSymbol(
        kind="ConfigFile",
        path=rel_path,
        qualified_name=f"config:{rel_path}",
        module_name=rel_path.replace("/", "."),
        signature=f"env_file {rel_path}",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(config_symbol)

    for idx, line in enumerate(lines, start=1):
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        if "=" in clean:
            var_name = clean.split("=", 1)[0].strip().lstrip("export ")
            if var_name and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", var_name):
                env_qualname = f"env:{var_name}"
                symbols.append(
                    ParsedSymbol(
                        kind="EnvVar",
                        path=rel_path,
                        qualified_name=env_qualname,
                        module_name=rel_path.replace("/", "."),
                        signature=f"env_var {var_name}",
                        location={"line": idx, "column": 0, "end_line": idx, "end_column": len(line)},
                        content_hash=hashlib.sha256(var_name.encode("utf-8")).hexdigest(),
                        owner_qualified_name=config_symbol.qualified_name,
                    )
                )
                relations.append(
                    ParsedRelation(
                        path=rel_path,
                        source_qualified_name=config_symbol.qualified_name,
                        relation="CONFIG_DEFINES_ENV",
                        target_name=env_qualname,
                        line=idx,
                    )
                )

    return symbols, relations
