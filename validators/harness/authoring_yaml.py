"""YAML parsing and repair for authored artifacts.

Handles YAML parsing, scalar block repair, and normalization of work order/contract
data structures. Fail-closed on parse errors.
"""

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
        # Whitespace is legal between the colon and the indicator
        # (`description: >`); omitting it made this repair a silent no-op.
        block_start = re.match(r"^(\s*)([a-zA-Z0-9_-]+)\s*:\s*[>|][+-]?\s*$", line)
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


ALLOWED_WORK_ORDER_KEYS = {
    "acceptance_criteria",
    "affected_services",
    "agent_assignments",
    "amended_at",
    "amended_by",
    "amendments",
    "api_changes",
    "assigned_agents",
    "blocked_by_rework",
    "blocked_reason",
    "cancellation_reason",
    "completed_at",
    "completion",
    "completion_history",
    "constraints",
    "context",
    "contract_ref",
    "created",
    "created_at",
    "db_migration",
    "deliverable",
    "deliverables",
    "dependencies",
    "dependency_graph",
    "description",
    "error",
    "evidence_revalidated_at",
    "evidence_stale",
    "files_created",
    "fix_verified",
    "frontend_changes",
    "gate_conditions",
    "id",
    "implementation_estimate",
    "instructions",
    "log",
    "milestone",
    "milestone_id",
    "milestones",
    "normalized_by",
    "notes",
    "phase_number",
    "plan_binding_hash",
    "plan_ref",
    "priority",
    "qa_by",
    "qa_verdict",
    "quality_gates",
    "references",
    "release",
    "release_commit",
    "release_tag",
    "released",
    "reopened_from",
    "reproduction_steps",
    "requirement_refs",
    "revalidation_history",
    "revision",
    "rework_budget",
    "rework_count",
    "root_cause",
    "scope",
    "skills",
    "status",
    "superseded_by",
    "synthesized_by",
    "test_plan",
    "title",
    "type",
    "unblocked",
    "unblocked_reason",
    "updated",
}


def normalize_work_order_data(
    data: dict[str, Any], project_root: Path | None = None
) -> dict[str, Any]:
    """Normalize common LLM shorthand/structure issues in work order dictionaries."""
    if not isinstance(data, dict):
        return data

    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict) and "acceptance_criteria" in deliverable:
        crit = deliverable.pop("acceptance_criteria")
        if "acceptance_criteria" not in data or not data["acceptance_criteria"]:
            data["acceptance_criteria"] = crit

    # Absorb revision notes / amendment reasons into amendments array
    note = (
        data.pop("revision_note", None)
        or data.pop("revision_notes", None)
        or data.pop("change_reason", None)
        or data.pop("amendment_reason", None)
        or data.pop("amendment_note", None)
        or data.pop("reason", None)
        or data.pop("comment", None)
    )
    if note:
        rev = data.get("revision", 1)
        if not isinstance(rev, int):
            try:
                rev = int(rev)
            except Exception:
                rev = 1
        amendments = data.setdefault("amendments", [])
        if isinstance(amendments, list):
            matched = False
            for a in amendments:
                if isinstance(a, dict) and a.get("revision") == rev:
                    if not a.get("reason") or a.get("reason") == "Address authoring readiness gate findings":
                        a["reason"] = str(note)
                    matched = True
                    break
            if not matched:
                amendments.append({
                    "revision": rev,
                    "reason": str(note),
                    "agent": str(data.get("amended_by") or "architect"),
                })

    # Normalize notes to list[str] if string
    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        data["notes"] = [notes.strip()]

    # Normalize acceptance_criteria if single string
    ac = data.get("acceptance_criteria")
    if isinstance(ac, str) and ac.strip():
        data["acceptance_criteria"] = [ac.strip()]
    elif isinstance(ac, list):
        data["acceptance_criteria"] = [str(x).strip() for x in ac if str(x).strip()]

    # Normalize assigned_agents to list[str] if string
    assigned = data.get("assigned_agents")
    if isinstance(assigned, str) and assigned.strip():
        data["assigned_agents"] = [assigned.strip()]

    # Normalize dependencies to list[str] if string
    deps = data.get("dependencies")
    if isinstance(deps, str):
        data["dependencies"] = [deps.strip()] if deps.strip() else []

    # Normalize revision to integer if numeric string
    if "revision" in data and not isinstance(data["revision"], int):
        try:
            data["revision"] = int(data["revision"])
        except (ValueError, TypeError):
            pass

    # Strip unexpected top-level keys that violate work-order.schema.json (additionalProperties: false)
    for k in list(data.keys()):
        if k not in ALLOWED_WORK_ORDER_KEYS:
            data.pop(k, None)

    # If deliverable is omitted when amending an existing work order on disk,
    # preserve the deliverable from the previous revision.
    wo_id = data.get("id")
    if (
        project_root
        and wo_id
        and not data.get("deliverable")
        and not data.get("deliverables")
        and not data.get("files_created")
    ):
        for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
            cand = Path(project_root) / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
            if not cand.is_file():
                cand = Path(project_root) / "work-orders" / sub / f"{wo_id}.yaml"
            if cand.is_file():
                try:
                    prior = yaml.safe_load(cand.read_text(encoding="utf-8"))
                    if isinstance(prior, dict) and prior.get("deliverable"):
                        data["deliverable"] = prior["deliverable"]
                        break
                except Exception:
                    pass

    return data


def normalize_contract_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM shorthand/structure issues in contract dictionaries."""
    if not isinstance(data, dict):
        return data

    # Map aliases for work order linkage
    if "wo_id" in data and "work_order" not in data:
        data["work_order"] = data.pop("wo_id")
    elif "work_order_id" in data and "work_order" not in data:
        data["work_order"] = data.pop("work_order_id")

    # Map agent aliases
    if "agent" in data and "agent_id" not in data:
        data["agent_id"] = data.pop("agent")

    # Strip unexpected fields that violate contract.schema.json (which has additionalProperties: false)
    ALLOWED_CONTRACT_KEYS = {
        "schema_version",
        "synthesized_by",
        "normalized_by",
        "agent_id",
        "work_order",
        "contract_revision",
        "guidelines",
        "skills",
        "identity",
        "scope",
        "budget",
    }
    for k in list(data.keys()):
        if k not in ALLOWED_CONTRACT_KEYS:
            data.pop(k, None)

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

    # 1. Role Authorization Check
    from .authoring_role import is_authorized, classify_artifact

    artifact_type = classify_artifact(norm_path)

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
        pass
    else:
        pass

    # 4. Overwrite Conflict Check
    effective_root = (
        Path(project_root).resolve() if project_root is not None else None
    )
    if effective_root is not None:
        from .authoring_conflict import _check_overwrite_conflict
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
        from .authoring_conflict import _session_authored_paths
        _session_authored_paths.add(norm_path)

    return {
        "passed": passed,
        "artifact_type": artifact_type,
        "path": norm_path,
        "author": author,
        "errors": errors,
        "normalized_content": normalized_content if passed else None,
    }
