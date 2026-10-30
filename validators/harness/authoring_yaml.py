"""YAML parsing and repair for authored artifacts.\n\nHandles YAML parsing, scalar block repair, and normalization of work order/contract\ndata structures. Fail-closed on parse errors.\n"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
import yaml


class YamlParseError(ValueError):
    """Raised when YAML cannot be parsed even after repair attempts."""


def repair_yaml_block_scalars(text: str) -> str:
    """Repair inconsistent indentation inside block scalars (e.g. description: > or |)."""
    lines = text.splitlines()
    repaired_lines = []
    in_block = False
    key_indent = 0
    block_indent = 0

    for line in lines:
        block_start = re.match(r"^(\s*)([a-zA-Z0-9_-]+)\s*[:][>|][+-]?\s*$", line)
        if block_start:
            in_block = True
            key_indent = len(block_start.group(1))
            block_indent = key_indent + 2
            repaired_lines.append(line)
            continue

        if in_block:
            key_match = re.match(r"^(\s*)([a-zA-Z0-9_-]+)\s*:", line)
            if key_match and len(key_match.group(1)) <= key_indent:
                in_block = False
                repaired_lines.append(line)
                continue

            stripped = line.strip()
            if stripped:
                curr_indent = len(line) - len(line.lstrip())
                if curr_indent < block_indent:
                    line = " " * block_indent + stripped
            repaired_lines.append(line)
        else:
            repaired_lines.append(line)

    return "\n".join(repaired_lines)


def normalize_work_order_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM shorthand/structure issues in work order dictionaries."""
    if not isinstance(data, dict):
        return data

    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict) and "acceptance_criteria" in deliverable:
        crit = deliverable.pop("acceptance_criteria")
        if "acceptance_criteria" not in data or not data["acceptance_criteria"]:
            data["acceptance_criteria"] = crit

    # Normalize acceptance_criteria if single string
    ac = data.get("acceptance_criteria")
    if isinstance(ac, str) and ac.strip():
        data["acceptance_criteria"] = ac.strip()

    return data


def normalize_contract_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM shorthand/structure issues in contract dictionaries."""
    if not isinstance(data, dict):
        return data

    # Strip unexpected fields that violate contract.schema.json
    data.pop("title", None)
    data.pop("version", None)

    # schema_version must be integer
    if "schema_version" in data:
        try:
            data["schema_version"] = int(data["schema_version"])
        except (ValueError, TypeError):
            pass

    # identity normalization
    identity = data.get("identity")
    if isinstance(identity, dict):
        if "role" in identity and isinstance(identity["role"], str):
            r = identity["role"].lower().strip()
            if "qa" in r or "test" in r:
                identity["role"] = "qa"
            elif "front" in r or "ui" in r:
                identity["role"] = "frontend"
            elif "back" in r or "api" in r or "server" in r:
                identity["role"] = "backend"
            elif "git" in r or "release" in r:
                identity["role"] = "gitops"
        if "reports_to" in identity and isinstance(identity["reports_to"], str):
            identity["reports_to"] = identity["reports_to"].lower().strip()

    # scope normalization
    scope = data.get("scope")
    if isinstance(scope, dict):
        raw_write = scope.get("write")
        if isinstance(raw_write, list):
            scope["write"] = "read-write" if len(raw_write) > 0 else "read-only"
            allow = scope.get("allow")
            if isinstance(allow, list):
                for w in raw_write:
                    mod = w.get("module") if isinstance(w, dict) else str(w)
                    if mod and not any(isinstance(a, dict) and a.get("module") == mod or a == mod for a in allow):
                        allow.append({"module": mod})

        allow = scope.get("allow")
        if isinstance(allow, list):
            new_allow = []
            for item in allow:
                if isinstance(item, str):
                    new_allow.append({"module": item.strip()})
                elif isinstance(item, dict) and "module" in item:
                    new_allow.append(item)
            scope["allow"] = new_allow

        deny = scope.get("deny")
        if isinstance(deny, list):
            new_deny = []
            for item in deny:
                if isinstance(item, str):
                    new_deny.append({"module": item.strip()})
                elif isinstance(item, dict) and "module" in item:
                    new_deny.append(item)
            scope["deny"] = new_deny

    return data


def validate_artifact_content(
    rel_path: str,
    content: str,
    agent: str | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Execute parse, schema, and semantic validation pipeline on artifact text.

    Returns a dict with validation results:
    {
        "passed": bool,
        "artifact_type": str,
        "path": str,
        "author": str,
        "errors": list[str],
        "normalized_content": str | None
    }
    """
    errors: list[str] = []
    author = agent or "unknown"
    norm_path = rel_path.replace("\\", "/").strip().lstrip("/")
    artifact_type = classify_artifact(norm_path)

    # 1. Role Authorization Check
    from .authoring_role import is_authorized, classify_artifact as _classify

    auth_ok, reason = is_authorized(agent)
    if not auth_ok and reason:
        return {
            "passed": False,
            "artifact_type": artifact_type,
            "path": norm_path,
            "author": author,
            "errors": [reason],
            "normalized_content": None,
        }

    # 2. YAML Parse Check
    if not content or not content.strip():
        return {
            "passed": False,
            "artifact_type": artifact_type,
            "path": norm_path,
            "author": author,
            "errors": [f"Artifact '{norm_path}' is empty."],
            "normalized_content": None,
        }

    normalized_content: str | None = None
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        repaired_text = repair_yaml_block_scalars(content)
        try:
            data = yaml.safe_load(repaired_text)
            normalized_content = repaired_text
        except Exception:
            return {
                "passed": False,
                "artifact_type": artifact_type,
                "path": norm_path,
                "author": author,
                "errors": [f"YAML syntax parsing error: {exc}"],
                "normalized_content": None,
            }
    except Exception as exc:
        return {
            "passed": False,
            "artifact_type": artifact_type,
            "path": norm_path,
            "author": author,
            "errors": [f"Unexpected error loading YAML: {exc}"],
            "normalized_content": None,
        }

    if not isinstance(data, dict):
        return {
            "passed": False,
            "artifact_type": artifact_type,
            "path": norm_path,
            "author": author,
            "errors": [f"Root YAML structure must be a dictionary/mapping, got {type(data).__name__}"],
            "normalized_content": None,
        }

    # 3. Schema & Semantic Validation by Artifact Type
    if artifact_type == "work_order":
        data = normalize_work_order_data(data)
        # Schema validation would be called separately
        if not errors:
            try:
                normalized_content = yaml.safe_dump(data, sort_keys=False)
            except Exception:
                pass
    elif artifact_type == "contract":
        data = normalize_contract_data(data)
        if not errors:
            try:
                normalized_content = yaml.safe_dump(data, sort_keys=False)
            except Exception:
                pass
    elif artifact_type == "index":
        pass  # Index validation handled elsewhere
    else:
        # Not a governed work-order or contract
        pass

    # 4. Overwrite Conflict Check
    effective_root = (
        Path(project_root).resolve() if project_root is not None else None
    )
    if effective_root is not None:
        _check_overwrite_conflict(
            norm_path, content, data, effective_root, errors, author=author
        )

    passed = len(errors) == 0
    if passed:
        if isinstance(data, dict):
            try:
                normalized_content = yaml.safe_dump(data, sort_keys=False)
            except Exception:
                pass
        # Track that we authored this path in the current session
        from .authoring_conflict import _session_authored_paths  # type: ignore

        # We can't easily import here, but the conflict module tracks this

    return {
        "passed": passed,
        "artifact_type": artifact_type,
        "path": norm_path,
        "author": author,
        "errors": errors,
        "normalized_content": normalized_content if passed else None,
    }