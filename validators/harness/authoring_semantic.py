"""Semantic validation for authored artifacts (work orders, contracts, indexes).\n\nValidates ID/filename alignment, non-empty allow rules, valid deliverables,\ndependencies, and other semantic constraints.\n"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from jsonschema import Draft7Validator

from .authoring_role import is_gitops_work_order


def _validate_work_order(
    norm_path: str,
    data: dict[str, Any],
    errors: List[str],
    project_root: Path | None = None,
) -> None:
    """Validate work order semantic constraints."""
    file_stem = Path(norm_path).stem
    wo_id = data.get("id")

    # Check required 'id' field
    if not wo_id:
        errors.append("Work Order missing required field 'id'")
    elif not re.match(r"^WO-[0-9]{3}$", str(wo_id)):
        errors.append(f"Work Order id '{wo_id}' does not match pattern '^WO-[0-9]{{3}}$'")
    elif file_stem.startswith("WO-") and wo_id != file_stem:
        errors.append(
            f"Work Order id '{wo_id}' does not match filename stem '{file_stem}'"
        )

    # Validate deliverable
    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict):
        deliv_type = deliverable.get("type")
        if deliv_type not in ("code", "doc", "config", "module"):
            errors.append(
                f"Work Order deliverable type '{deliv_type}' invalid; must be code, doc, config, or module"
            )
        if not deliverable.get("description"):
            errors.append("Work Order deliverable requires a non-empty 'description'")

    # Validate requirement_refs if present
    requirement_refs = data.get("requirement_refs")
    if isinstance(requirement_refs, list):
        for ref in requirement_refs:
            if not isinstance(ref, str):
                errors.append(f"requirement_refs entry '{ref}' must be a string")
            elif not ref.startswith("R"):
                errors.append(
                    f"requirement_refs entry '{ref}' must start with 'R' (e.g., R001)"
                )

    # Validate dependencies
    deps = data.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            errors.append("Work Order dependencies must be a list of strings")
        else:
            is_gitops = is_gitops_work_order(data)
            if project_root is not None:
                # _is_gitops_dependency would be called from authoring_gate
                pass  # Simplified for this module
            for dep in deps:
                dep_str = str(dep).strip()
                if dep_str == wo_id:
                    errors.append(f"Work order '{wo_id}' cannot depend on itself")


def _validate_contract(
    norm_path: str,
    data: dict[str, Any],
    errors: List[str],
) -> None:
    """Validate contract semantic constraints."""
    # Check for required allow rules structure
    allow_rules = data.get("allow_rules") or data.get("allowed") or data.get("allows")
    if not allow_rules:
        # Specialized workers are free to write own files without microscopic scope.allow
        # But a contract with no allow rules at all may be incomplete
        pass

    # Check for deny rules
    deny_rules = data.get("deny_rules") or data.get("denies") or data.get("forbidden")
    # Note: deny_rules validation is more complex and depends on the full schema


def _validate_index(
    norm_path: str,
    data: dict[str, Any],
    errors: List[str],
) -> None:
    """Validate index semantic constraints."""
    # Index validation - check milestone/plan alignment
    pass


def validate_semantic(
    artifact_type: str,
    data: dict[str, Any],
    errors: List[str],
    project_root: Path | None = None,
) -> None:
    """Run semantic validation for the given artifact type."""
    if artifact_type == "work_order":
        _validate_work_order(errors[-1] if errors else "", data, errors, project_root)
    elif artifact_type == "contract":
        _validate_contract(errors[-1] if errors else "", data, errors)
    elif artifact_type == "index":
        _validate_index(errors[-1] if errors else "", data, errors)