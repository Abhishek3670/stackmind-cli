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

def repair_unquoted_yaml_scalars(text: str) -> str:
    """Repair unquoted YAML string fields containing unquoted colons or multiline text.

    LLMs often author descriptions or instructions containing colons (e.g.
    'description: Build behavior. EXTRACT PURE LOGIC: expose a global...') without
    quoting or block indicators, which causes yaml.scanner.ScannerError: 'mapping
    values are not allowed here'. This converts such fields into folded block scalars.
    """
    lines = text.splitlines()
    out = []
    keys = {
        "description",
        "instructions",
        "notes",
        "cancellation_reason",
        "blocked_reason",
        "unblocked_reason",
        "guidelines",
        "reason",
        "comment",
        "title",
    }
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s*)([a-zA-Z0-9_-]+)\s*:\s*(.*)$", line)
        if m:
            indent, key, val = m.group(1), m.group(2), m.group(3).strip()
            key_indent_len = len(indent)
            if key in keys and not val.startswith(('"', "'", ">", "|", "[", "{")):
                has_colon = ": " in val
                has_indented_next = (
                    i + 1 < len(lines)
                    and re.match(r"^(\s+)\S", lines[i + 1])
                    and len(re.match(r"^(\s+)\S", lines[i + 1]).group(1)) > key_indent_len
                    and not re.match(r"^\s*[a-zA-Z0-9_-]+\s*:", lines[i + 1])
                )
                if has_colon or has_indented_next or not val:
                    block_lines = []
                    if val:
                        block_lines.append(f"{indent}  {val}")
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        nm = re.match(r"^(\s*)([a-zA-Z0-9_-]+)\s*:", nxt)
                        if nm and len(nm.group(1)) <= key_indent_len:
                            break
                        strip_nxt = nxt.strip()
                        if strip_nxt:
                            block_lines.append(f"{indent}  {strip_nxt}")
                        else:
                            block_lines.append("")
                        i += 1
                    out.append(f"{indent}{key}: >-")
                    out.extend(block_lines)
                    continue
        out.append(line)
        i += 1
    return "\n".join(out)



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

    # Normalize work-order type
    WO_TYPE_MAP = {
        "QA": "VALIDATION",
        "TEST": "VALIDATION",
        "TESTING": "VALIDATION",
        "VERIFICATION": "VALIDATION",
        "REVIEW": "VALIDATION",
    }
    raw_type = data.get("type")
    if isinstance(raw_type, str):
        cleaned_type = raw_type.strip().upper()
        if cleaned_type in WO_TYPE_MAP:
            data["type"] = WO_TYPE_MAP[cleaned_type]
        else:
            data["type"] = cleaned_type
    elif not raw_type:
        data["type"] = "FEATURE"

    # Normalize deliverable.type and deliverables[].type
    DELIVERABLE_TYPE_MAP = {
        "report": "doc",
        "documentation": "doc",
        "document": "doc",
        "markdown": "doc",
        "audit": "doc",
        "review": "doc",
        "test": "code",
        "tests": "code",
        "testing": "code",
        "script": "code",
        "scripts": "code",
        "configuration": "config",
        "conf": "config",
    }
    raw_deliv = data.get("deliverable")
    if isinstance(raw_deliv, str) and raw_deliv.strip():
        p = raw_deliv.strip()
        dtype = "doc" if p.endswith((".md", ".txt", ".rst")) else ("config" if p.endswith((".json", ".yaml", ".yml", ".toml")) else "code")
        data["deliverable"] = {
            "path": p,
            "type": dtype,
            "description": data.get("description") or f"Deliverable {p} for {data.get('title', 'work order')}",
        }
    elif isinstance(raw_deliv, dict):
        deliv = raw_deliv
        dtype = deliv.get("type")
        if isinstance(dtype, str) and dtype.lower().strip() in DELIVERABLE_TYPE_MAP:
            deliv["type"] = DELIVERABLE_TYPE_MAP[dtype.lower().strip()]
        elif not dtype or not isinstance(dtype, str):
            p = str(deliv.get("path", "")).strip()
            deliv["type"] = "doc" if p.endswith((".md", ".txt", ".rst")) else ("config" if p.endswith((".json", ".yaml", ".yml", ".toml")) else "code")
        if not deliv.get("description") or not str(deliv.get("description")).strip():
            deliv["description"] = data.get("description") or f"Deliverable for {data.get('title', data.get('id', 'work order'))}"
    elif not raw_deliv:
        delivs = data.get("deliverables")
        if isinstance(delivs, list) and delivs and isinstance(delivs[0], dict):
            first = dict(delivs[0])
            dtype = first.get("type")
            if isinstance(dtype, str) and dtype.lower().strip() in DELIVERABLE_TYPE_MAP:
                first["type"] = DELIVERABLE_TYPE_MAP[dtype.lower().strip()]
            elif not dtype:
                first["type"] = "code"
            if not first.get("description"):
                first["description"] = data.get("description") or f"Deliverable for {data.get('title', 'work order')}"
            data["deliverable"] = first
        else:
            data["deliverable"] = {
                "type": "code",
                "description": data.get("description") or f"Deliverable for {data.get('title', data.get('id', 'work order'))}",
            }

    deliverables = data.get("deliverables")
    if isinstance(deliverables, list):
        for item in deliverables:
            if isinstance(item, dict):
                dtype = item.get("type")
                if isinstance(dtype, str) and dtype.lower().strip() in DELIVERABLE_TYPE_MAP:
                    item["type"] = DELIVERABLE_TYPE_MAP[dtype.lower().strip()]

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

    # Normalize assigned_agents
    if not data.get("assigned_agents"):
        for alias_key in ("agent", "assigned_agent", "owner", "assigned_to", "assignee", "assigned"):
            val = data.get(alias_key)
            if isinstance(val, str) and val.strip():
                data["assigned_agents"] = [val.strip()]
                break
            elif isinstance(val, list) and val:
                data["assigned_agents"] = [str(x).strip() for x in val if str(x).strip()]
                break
    assigned = data.get("assigned_agents")
    if isinstance(assigned, str) and assigned.strip():
        data["assigned_agents"] = [assigned.strip()]
    elif isinstance(assigned, list):
        data["assigned_agents"] = [str(x).strip() for x in assigned if str(x).strip()]
    if not data.get("assigned_agents"):
        deliv = data.get("deliverable")
        path_str = str(deliv.get("path") if isinstance(deliv, dict) else (deliv or "")).lower()
        wo_type = str(data.get("type", "")).upper()
        if wo_type == "VALIDATION":
            data["assigned_agents"] = ["gemma"]
        elif any(ext in path_str for ext in (".html", ".css", "assets/", "style", "ui")):
            data["assigned_agents"] = ["gemini"]
        elif wo_type in ("AUDIT", "RESEARCH") or any(ext in path_str for ext in ("version", "changelog", "readme")):
            data["assigned_agents"] = ["local-llm"]
        else:
            data["assigned_agents"] = ["codex"]

    # Normalize dependencies
    deps = data.get("dependencies")
    if isinstance(deps, str):
        data["dependencies"] = [deps.strip()] if deps.strip() else []
    elif isinstance(deps, list):
        data["dependencies"] = [str(x).strip() for x in deps if str(x).strip()]

    # Normalize priority
    pri = data.get("priority")
    if isinstance(pri, str):
        pri_clean = pri.strip().upper()
        if pri_clean in ("P0", "P1", "P2", "P3"):
            data["priority"] = pri_clean

    # Normalize status
    stat = data.get("status")
    if isinstance(stat, str):
        stat_clean = stat.strip().upper()
        if stat_clean == "IN_PROGRESS":
            stat_clean = "ACTIVE"
        elif stat_clean == "DONE":
            stat_clean = "COMPLETED"
        data["status"] = stat_clean

    # Normalize revision to integer if numeric string
    if "revision" in data and not isinstance(data["revision"], int):
        try:
            data["revision"] = int(data["revision"])
        except (ValueError, TypeError):
            pass

    # Normalize amendments array to strictly conform to work-order.schema.json
    raw_amendments = data.get("amendments")
    if isinstance(raw_amendments, list):
        clean_amendments = []
        for idx_am, item in enumerate(raw_amendments):
            if isinstance(item, str):
                item = {"reason": item, "revision": data.get("revision", idx_am + 1)}
            elif not isinstance(item, dict):
                continue
            item = dict(item)
            if "author" in item and "agent" not in item:
                item["agent"] = str(item.pop("author"))
            if "timestamp" in item and "date" not in item:
                item["date"] = str(item.pop("timestamp"))
            if "time" in item and "date" not in item:
                item["date"] = str(item.pop("time"))
            if "entry" in item and "reason" not in item:
                item["reason"] = str(item.pop("entry"))
            if "note" in item and "reason" not in item:
                item["reason"] = str(item.pop("note"))
            if "description" in item and "reason" not in item:
                item["reason"] = str(item.pop("description"))

            if "revision" not in item or not isinstance(item["revision"], int):
                try:
                    item["revision"] = int(item["revision"])
                except Exception:
                    item["revision"] = int(data.get("revision", idx_am + 1) or 1)
            if not item.get("reason"):
                item["reason"] = f"Revision {item['revision']} amendment"
            else:
                item["reason"] = str(item["reason"])

            ALLOWED_AMENDMENT_KEYS = {"revision", "agent", "date", "reason", "changed_fields"}
            for k in list(item.keys()):
                if k not in ALLOWED_AMENDMENT_KEYS:
                    item.pop(k, None)
            clean_amendments.append(item)
        data["amendments"] = clean_amendments

    # Normalize log array to conform to work-order.schema.json
    raw_log = data.get("log")
    if isinstance(raw_log, list):
        clean_log = []
        for item in raw_log:
            if isinstance(item, str):
                clean_log.append(item)
            elif isinstance(item, dict):
                d = dict(item)
                date_val = d.pop("date", None) or d.pop("timestamp", None) or d.pop("time", None) or "2026-11-04T00:00:00Z"
                agent_val = d.pop("agent", None) or d.pop("author", None) or "architect"
                action_val = d.pop("action", None) or d.pop("entry", None) or d.pop("message", None) or d.pop("note", None) or "log entry"
                clean_log.append({
                    "date": str(date_val),
                    "agent": str(agent_val),
                    "action": str(action_val),
                })
        data["log"] = clean_log

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

    budget = data.get("budget")
    if isinstance(budget, dict):
        ALLOWED_BUDGET_KEYS = {"max_files_touched", "max_tokens", "expires_at"}
        for k in list(budget.keys()):
            if k not in ALLOWED_BUDGET_KEYS:
                budget.pop(k, None)
        if "max_files_touched" in budget:
            try:
                budget["max_files_touched"] = max(1, int(budget["max_files_touched"]))
            except Exception:
                pass

    return data


def normalize_index_data(
    data: dict[str, Any], project_root: Path | None = None
) -> dict[str, Any]:
    """Normalize common LLM shorthand/structure issues in work order index dictionaries."""
    if not isinstance(data, dict):
        return data

    if "schema_version" not in data:
        data["schema_version"] = 1
    elif not isinstance(data["schema_version"], int):
        try:
            data["schema_version"] = int(data["schema_version"])
        except Exception:
            data["schema_version"] = 1

    # Map work_orders / items / orders_list -> orders
    if "orders" not in data:
        if isinstance(data.get("work_orders"), list):
            data["orders"] = data.pop("work_orders")
        elif isinstance(data.get("items"), list):
            data["orders"] = data.pop("items")
        elif isinstance(data.get("orders_list"), list):
            data["orders"] = data.pop("orders_list")
        else:
            data["orders"] = []

    if isinstance(data.get("orders"), list):
        clean_orders = []
        for o in data["orders"]:
            if isinstance(o, dict):
                o = normalize_work_order_data(o, project_root=project_root)
                if "id" not in o and "wo_id" in o:
                    o["id"] = o.pop("wo_id")
                if "status" not in o:
                    o["status"] = "READY"
                if "title" not in o:
                    o["title"] = f"Work Order {o.get('id', '')}".strip()
                clean_orders.append(o)
        data["orders"] = clean_orders

    if "next_id" not in data or not isinstance(data.get("next_id"), int):
        max_id = 0
        for o in data.get("orders", []):
            if isinstance(o, dict) and "id" in o:
                m = re.search(r"(\d+)", str(o["id"]))
                if m:
                    max_id = max(max_id, int(m.group(1)))
        data["next_id"] = max_id + 1

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
            repaired_text2 = repair_unquoted_yaml_scalars(repaired_text)
            try:
                data = yaml.safe_load(repaired_text2)
                normalized_content = repaired_text2
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
        data = normalize_index_data(data, project_root=project_root)
        if not errors:
            try:
                normalized_content = yaml.safe_dump(data, sort_keys=False)
            except Exception:
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
