"""CI/CD compiler frontend augmentation for the knowledge compiler.

Parses CI/CD workflow pipeline files (.github/workflows/*.yml, .gitlab-ci.yml)
to extract workflow pipelines, jobs, steps, run commands, and job dependencies.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .parse import DEFAULT_EXCLUDED_DIRS, ParsedFile, ParsedRelation, ParsedSymbol

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

EXCLUDED_DIRS = DEFAULT_EXCLUDED_DIRS


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with CI/CD pipeline workflow symbols and relations."""
    if project_path is None:
        return

    project_path = project_path.resolve()
    parsed_by_path = {p.path: p for p in parsed_files}

    # GitHub Actions workflows (.github/workflows/*.yml, *.yaml)
    workflows_dir = project_path / ".github" / "workflows"
    if workflows_dir.exists():
        for wf_path in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
            rel = wf_path.relative_to(project_path).as_posix()
            _process_cicd_file(rel, wf_path, _compile_github_workflow, parsed_files, parsed_by_path)

    # GitLab CI (.gitlab-ci.yml)
    gitlab_path = project_path / ".gitlab-ci.yml"
    if gitlab_path.exists():
        rel = gitlab_path.relative_to(project_path).as_posix()
        _process_cicd_file(rel, gitlab_path, _compile_gitlab_ci, parsed_files, parsed_by_path)


def _process_cicd_file(
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


def _compile_github_workflow(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    wf_name = Path(rel_path).stem
    triggers_str = "push"

    parsed_data = None
    if yaml is not None:
        try:
            parsed_data = yaml.safe_load(content)
            if isinstance(parsed_data, dict):
                wf_name = str(parsed_data.get("name") or wf_name)
                on_val = parsed_data.get("on")
                if isinstance(on_val, list):
                    triggers_str = ",".join(str(x) for x in on_val)
                elif isinstance(on_val, dict):
                    triggers_str = ",".join(on_val.keys())
                elif isinstance(on_val, str):
                    triggers_str = on_val
        except Exception:
            pass

    pipeline_symbol = ParsedSymbol(
        kind="Pipeline",
        path=rel_path,
        qualified_name=f"pipeline:{rel_path}",
        module_name=rel_path.replace("/", "."),
        signature=f"pipeline {wf_name} on=[{triggers_str}]",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(pipeline_symbol)

    if isinstance(parsed_data, dict) and "jobs" in parsed_data and isinstance(parsed_data["jobs"], dict):
        for job_id, job_data in parsed_data["jobs"].items():
            if not isinstance(job_data, dict):
                continue

            runs_on = str(job_data.get("runs-on", "ubuntu-latest"))
            job_qualname = f"job:{rel_path}:{job_id}"

            job_symbol = ParsedSymbol(
                kind="PipelineJob",
                path=rel_path,
                qualified_name=job_qualname,
                module_name=rel_path.replace("/", "."),
                signature=f"job {job_id} runs-on={runs_on}",
                location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                content_hash=hashlib.sha256(job_id.encode("utf-8")).hexdigest(),
                owner_qualified_name=pipeline_symbol.qualified_name,
            )
            symbols.append(job_symbol)

            relations.append(
                ParsedRelation(
                    path=rel_path,
                    source_qualified_name=pipeline_symbol.qualified_name,
                    relation="PIPELINE_CONTAINS_JOB",
                    target_name=job_qualname,
                    line=1,
                )
            )

            # Job dependencies (`needs:`)
            needs_val = job_data.get("needs")
            needed_jobs = [needs_val] if isinstance(needs_val, str) else (needs_val if isinstance(needs_val, list) else [])
            for needed in needed_jobs:
                needed_qualname = f"job:{rel_path}:{needed}"
                relations.append(
                    ParsedRelation(
                        path=rel_path,
                        source_qualified_name=job_qualname,
                        relation="JOB_DEPENDS_ON_JOB",
                        target_name=needed_qualname,
                        line=1,
                    )
                )

            # Steps under job
            steps = job_data.get("steps", [])
            if isinstance(steps, list):
                for idx, step_data in enumerate(steps, start=1):
                    if not isinstance(step_data, dict):
                        continue
                    step_name = str(step_data.get("name") or step_data.get("uses") or f"step-{idx}")
                    step_run = str(step_data.get("run", ""))
                    step_qualname = f"step:{rel_path}:{job_id}:{idx}"

                    step_symbol = ParsedSymbol(
                        kind="PipelineStep",
                        path=rel_path,
                        qualified_name=step_qualname,
                        module_name=rel_path.replace("/", "."),
                        signature=f"step {step_name}".strip(),
                        location={"line": idx, "column": 0, "end_line": idx, "end_column": 0},
                        content_hash=hashlib.sha256(f"{step_name}{step_run}".encode("utf-8")).hexdigest(),
                        owner_qualified_name=job_qualname,
                    )
                    symbols.append(step_symbol)

                    relations.append(
                        ParsedRelation(
                            path=rel_path,
                            source_qualified_name=job_qualname,
                            relation="JOB_CONTAINS_STEP",
                            target_name=step_qualname,
                            line=idx,
                        )
                    )

                    if step_run:
                        cmd = step_run.split()[0]
                        relations.append(
                            ParsedRelation(
                                path=rel_path,
                                source_qualified_name=step_qualname,
                                relation="STEP_RUNS_COMMAND",
                                target_name=cmd,
                                line=idx,
                            )
                        )

    return symbols, relations


def _compile_gitlab_ci(rel_path: str, content: str) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    lines = content.splitlines()
    total_lines = len(lines) or 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    pipeline_symbol = ParsedSymbol(
        kind="Pipeline",
        path=rel_path,
        qualified_name=f"pipeline:{rel_path}",
        module_name="gitlab-ci",
        signature="pipeline gitlab-ci",
        location={"line": 1, "column": 0, "end_line": total_lines, "end_column": 0},
        content_hash=content_hash,
        owner_qualified_name=None,
    )
    symbols.append(pipeline_symbol)

    return symbols, relations
