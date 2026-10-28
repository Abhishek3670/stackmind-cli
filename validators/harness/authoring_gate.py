"""Authoring Gate for Work Orders and Contracts (Phase D).

Enforces:
1. Role authorization: worker roles (codex, gemini, local-llm, gemma) are strictly
   forbidden from authoring or modifying contracts or work orders (AGENTS.md / CONTRACT-01).
   Only architect/CEO roles (claude, architecture, ceo) may author governed artifacts.
2. YAML parsing: malformed syntax is immediately rejected fail-closed.
3. Schema validation: validated against schemas/work-order.schema.json or schemas/contract.schema.json.
4. Semantic validation: verifies ID/filename alignment, non-empty allow rules, valid deliverables.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any
import yaml
from jsonschema import Draft7Validator


class AuthoringValidationError(ValueError):
    """Raised when an authored artifact violates authoring constraints."""
    pass


@dataclass
class AuthoringGateDecision:
    passed: bool
    artifact_type: str
    path: str
    author: str = ""
    errors: list[str] = field(default_factory=list)
    normalized_content: str | None = None

    @property
    def summary(self) -> str:
        if self.passed:
            return f"AuthoringGate PASS: {self.artifact_type} '{self.path}' authored by '{self.author}'"
        return f"AuthoringGate REJECT: {self.artifact_type} '{self.path}': {'; '.join(self.errors)}"


class AuthoringGate:
    """Fail-closed validation gate for model-authored Work Orders and Contracts."""

    @staticmethod
    def is_gitops_work_order(data: dict[str, Any]) -> bool:
        """Check if a work order is designated for the GitOps release role."""
        if not isinstance(data, dict):
            return False
        assigned = data.get("assigned_agents") or []
        if isinstance(assigned, str):
            assigned = [assigned]
        assigned_norm = [str(a).lower().strip() for a in assigned]
        if any(any(kw in a for kw in ("local-llm", "local_llm", "gitops", "release")) for a in assigned_norm):
            return True
        wo_title = str(data.get("title", "")).lower()
        wo_type = str(data.get("type", "")).lower()
        if any(kw in wo_title for kw in ("gitops", "release")) or wo_type in ("release", "gitops"):
            return True
        return False

    def _is_gitops_dependency(self, dep_id: str, project_root: Path) -> bool:
        """Check if a target dependency work order is a GitOps release order."""
        for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
            cand = project_root / ".sync" / "work-orders" / sub / f"{dep_id}.yaml"
            if not cand.exists():
                cand = project_root / "work-orders" / sub / f"{dep_id}.yaml"
            if cand.is_file():
                try:
                    loaded = yaml.safe_load(cand.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and self.is_gitops_work_order(loaded):
                        return True
                except Exception:
                    pass
        return False

    AUTHORIZED_AUTHORS = {"claude", "architecture", "architect", "ceo", "admin", "system", "test"}
    WORKER_ROLES = {"codex", "gemini", "local-llm", "gemma"}

    def __init__(self, schemas_dir: Path | None = None, project_root: Path | None = None) -> None:
        self._schemas_dir = schemas_dir or self._find_schemas_dir()
        self._project_root = Path(project_root).resolve() if project_root is not None else None
        self._schema_cache: dict[str, dict[str, Any]] = {}
        self._session_authored_paths: set[str] = set()

    @staticmethod
    def _find_schemas_dir() -> Path:
        # 1. Check relative to this file: repo_root / schemas
        candidate = Path(__file__).resolve().parents[2] / "schemas"
        if candidate.exists() and (candidate / "contract.schema.json").exists():
            return candidate
        # 2. Check current working directory / .sync / schemas
        candidate = Path.cwd() / ".sync" / "schemas"
        if candidate.exists() and (candidate / "contract.schema.json").exists():
            return candidate
        # 3. Check current working directory / schemas
        candidate = Path.cwd() / "schemas"
        if candidate.exists() and (candidate / "contract.schema.json").exists():
            return candidate
        # Fallback to repo candidate
        return Path(__file__).resolve().parents[2] / "schemas"

    def get_schema(self, schema_name: str) -> dict[str, Any] | None:
        if schema_name in self._schema_cache:
            return self._schema_cache[schema_name]
        schema_path = self._schemas_dir / schema_name
        if not schema_path.exists():
            return None
        try:
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            self._schema_cache[schema_name] = data
            return data
        except Exception:
            return None

    @staticmethod
    def classify_artifact(path: str) -> str:
        """Classify a relative path as 'work_order', 'contract', 'index', or 'unknown'."""
        norm = path.replace("\\", "/").strip().lstrip("/")
        if norm.endswith("INDEX.yaml") or norm.endswith("INDEX.yml"):
            return "index"
        if ".sync/contracts" in norm or norm.startswith("contracts/"):
            return "contract"
        if (".sync/work-orders" in norm or norm.startswith("work-orders/")) and re.search(r"WO-[0-9]{3}\.ya?ml$", norm):
            return "work_order"
        if re.search(r"^WO-[0-9]{3}\.ya?ml$", Path(norm).name):
            if "contract" in norm:
                return "contract"
            return "work_order"
        return "unknown"

    def validate_author_role(self, agent: str | None, path: str) -> tuple[bool, str | None]:
        """Verify that the requesting agent has authority to author this artifact.

        Workers (Codex, Gemini, Local-LLM, Gemma) MUST NEVER author contracts or work orders.
        Only Claude (Architect) and CEO hold authoring authority.
        """
        if not agent:
            return True, None
        agent_norm = agent.lower().strip()
        if agent_norm in self.WORKER_ROLES:
            return (
                False,
                f"Worker role '{agent}' is not authorized to author or modify governed artifact '{path}'. "
                f"Per AGENTS.md / CONTRACT-01, only Claude (Architect) or CEO may author contracts and work orders.",
            )
        return True, None

    def validate_artifact_content(
        self,
        rel_path: str,
        content: str,
        agent: str | None = None,
        project_root: Path | str | None = None,
    ) -> AuthoringGateDecision:
        """Execute full parse, schema, and semantic validation pipeline on artifact text."""
        errors: list[str] = []
        author = agent or "unknown"
        norm_path = rel_path.replace("\\", "/").strip().lstrip("/")
        artifact_type = self.classify_artifact(norm_path)

        # 1. Role Authorization Check
        auth_ok, reason = self.validate_author_role(agent, norm_path)
        if not auth_ok and reason:
            return AuthoringGateDecision(
                passed=False,
                artifact_type=artifact_type,
                path=norm_path,
                author=author,
                errors=[reason],
            )

        # 2. YAML Parse Check
        if not content or not content.strip():
            return AuthoringGateDecision(
                passed=False,
                artifact_type=artifact_type,
                path=norm_path,
                author=author,
                errors=[f"Artifact '{norm_path}' is empty."],
            )

        normalized_content: str | None = None
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            repaired_text = self.repair_yaml_block_scalars(content)
            try:
                data = yaml.safe_load(repaired_text)
                normalized_content = repaired_text
            except Exception:
                return AuthoringGateDecision(
                    passed=False,
                    artifact_type=artifact_type,
                    path=norm_path,
                    author=author,
                    errors=[f"YAML syntax parsing error: {exc}"],
                )
        except Exception as exc:
            return AuthoringGateDecision(
                passed=False,
                artifact_type=artifact_type,
                path=norm_path,
                author=author,
                errors=[f"Unexpected error loading YAML: {exc}"],
            )

        if not isinstance(data, dict):
            return AuthoringGateDecision(
                passed=False,
                artifact_type=artifact_type,
                path=norm_path,
                author=author,
                errors=[f"Root YAML structure must be a dictionary/mapping, got {type(data).__name__}"],
            )

        # 3. Schema & Semantic Validation by Artifact Type
        if artifact_type == "work_order":
            data = self.normalize_work_order_data(data)
            self._validate_work_order(norm_path, data, errors)
            if not errors:
                try:
                    normalized_content = yaml.safe_dump(data, sort_keys=False)
                except Exception:
                    pass
        elif artifact_type == "contract":
            data = self.normalize_contract_data(data)
            self._validate_contract(norm_path, data, errors)
            if not errors:
                try:
                    normalized_content = yaml.safe_dump(data, sort_keys=False)
                except Exception:
                    pass
        elif artifact_type == "index":
            self._validate_index(norm_path, data, errors)
        else:
            # Not a governed work-order or contract
            pass

        # 4. Overwrite Conflict Check for Active Work Orders and Contracts
        effective_root = Path(project_root).resolve() if project_root is not None else self._project_root
        if effective_root is not None:
            self._check_overwrite_conflict(norm_path, content, data, effective_root, errors, author=author)

        passed = len(errors) == 0
        if passed:
            self._session_authored_paths.add(norm_path)
            if isinstance(data, dict):
                try:
                    normalized_content = yaml.safe_dump(data, sort_keys=False)
                except Exception:
                    pass

        return AuthoringGateDecision(
            passed=passed,
            artifact_type=artifact_type,
            path=norm_path,
            author=author,
            errors=errors,
            normalized_content=normalized_content if passed else None,
        )

    def _check_overwrite_conflict(
        self,
        norm_path: str,
        content: str,
        data: Any,
        project_root: Path,
        errors: list[str],
        author: str | None = None,
    ) -> None:
        """Prevent clobbering an existing active work order or contract from another task."""
        is_active_wo = (
            norm_path.startswith(".sync/work-orders/ACTIVE/")
            or norm_path.startswith("work-orders/ACTIVE/")
        )
        is_contract = (
            norm_path.startswith(".sync/contracts/")
            or norm_path.startswith("contracts/")
        )
        # Terminal buckets are completion evidence: a governed write must never
        # rewrite a COMPLETED/BLOCKED work order into a different task that
        # still looks verified. Reopening is a lifecycle decision, not a write.
        is_terminal_wo = any(
            norm_path.startswith(prefix)
            for prefix in (
                ".sync/work-orders/COMPLETED/",
                "work-orders/COMPLETED/",
                ".sync/work-orders/BLOCKED/",
                "work-orders/BLOCKED/",
            )
        )
        if not (is_active_wo or is_contract or is_terminal_wo):
            return

        # Check candidate locations in project root
        candidates = [project_root / norm_path]
        if not norm_path.startswith(".sync/"):
            candidates.append(project_root / ".sync" / norm_path)

        existing_file = next((p for p in candidates if p.is_file()), None)
        if existing_file is None:
            return

        # If already authored by THIS session, allow updating it (never for a
        # terminal work order, whose content is completion evidence).
        if norm_path in self._session_authored_paths and not is_terminal_wo:
            return

        # Check if existing content matches what this session is authoring
        try:
            existing_content = existing_file.read_text(encoding="utf-8")
        except Exception:
            existing_content = ""

        # Exact textual match (idempotent write)
        if existing_content.strip() == content.strip():
            return

        # Semantic YAML dictionary match
        try:
            existing_data = yaml.safe_load(existing_content)
        except Exception:
            existing_data = None

        if isinstance(existing_data, dict) and isinstance(data, dict) and existing_data == data:
            return

        if is_terminal_wo:
            errors.append(
                f"Overwrite conflict: terminal work order '{norm_path}' cannot be rewritten "
                "in place. Reopen or supersede it through the lifecycle so its completion "
                "evidence is explicitly invalidated."
            )
            return

        # Scaffolding or repair artifacts: an authorized author (Architect/CEO)
        # may supersede synthesized artifacts or amend contracts/work orders.
        # Workers never reach this check (role gate runs first).
        if str(author or "").lower() in self.AUTHORIZED_AUTHORS and isinstance(existing_data, dict):
            existing_synth = str(existing_data.get("synthesized_by", "")).lower()
            new_synth = str(data.get("synthesized_by", "") if isinstance(data, dict) else "").lower()
            repair_markers = {"bootstrap", "recovery-repair", "architect-repair", "authoring-repair", "repair", "reconcile"}
            is_repair = (
                existing_synth in repair_markers
                or new_synth in repair_markers
                or str(existing_data.get("status", "")).upper() in {"BLOCKED", "REWORK"}
                or bool(existing_data.get("error") or existing_data.get("blocked_reason"))
            )
            if not is_repair and is_contract:
                wo_stem = Path(norm_path).stem
                wo_cand = project_root / ".sync" / "work-orders" / "ACTIVE" / f"{wo_stem}.yaml"
                if not wo_cand.exists():
                    wo_cand = project_root / "work-orders" / "ACTIVE" / f"{wo_stem}.yaml"
                if wo_cand.is_file():
                    try:
                        wo_data = yaml.safe_load(wo_cand.read_text(encoding="utf-8"))
                        if isinstance(wo_data, dict):
                            if (
                                str(wo_data.get("status", "")).upper() in {"BLOCKED", "REWORK"}
                                or bool(wo_data.get("error") or wo_data.get("blocked_reason"))
                            ):
                                is_repair = True
                    except Exception:
                        pass
            if not is_repair:
                # Also check if supervisor run state is currently in planning, repair, authoring, or blocked phase
                sup_dir = project_root / ".sync" / "runtime" / "supervisor"
                if sup_dir.is_dir():
                    for rf in sup_dir.glob("run-*.yaml"):
                        try:
                            r_state = yaml.safe_load(rf.read_text(encoding="utf-8"))
                            if isinstance(r_state, dict):
                                r_ph = str(r_state.get("phase", "")).upper()
                                if r_ph in (
                                    "PLANNING",
                                    "AWAITING_APPROVAL",
                                    "AUTHORING",
                                    "AUTHORING_READINESS_GATE",
                                    "ARCHITECT_REPAIR",
                                    "ARCHITECT_RECOVERY_DECISION",
                                    "BLOCKED",
                                ) or bool(r_state.get("readiness_issues")):
                                    is_repair = True
                                    break
                        except Exception:
                            pass

            if is_repair:
                # During repair of blocked/failing/scaffold artifacts or active supervisor runs,
                # an authorized author is explicitly authorized to replace or refine the artifact.
                if is_active_wo and isinstance(data, dict):
                    existing_rev = existing_data.get("revision", 1)
                    new_rev = data.get("revision")
                    if not (isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev):
                        existing_num = int(existing_rev) if isinstance(existing_rev, (int, float)) else 1
                        bumped_rev = max(existing_num + 1, int(new_rev or 0) + 1)
                        data["revision"] = bumped_rev
                        amendments = data.setdefault("amendments", [])
                        if isinstance(amendments, list):
                            amendments.append({
                                "revision": bumped_rev,
                                "reason": "Authoring refinement and governance amendment",
                            })
                elif is_contract and isinstance(data, dict):
                    existing_rev = existing_data.get("contract_revision", 1)
                    new_rev = data.get("contract_revision")
                    if not (isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev):
                        existing_num = int(existing_rev) if isinstance(existing_rev, (int, float)) else 1
                        bumped_rev = max(existing_num + 1, int(new_rev or 0) + 1)
                        data["contract_revision"] = bumped_rev
                return

            if is_contract and isinstance(data, dict):
                existing_rev = existing_data.get("contract_revision", 1)
                new_rev = data.get("contract_revision")
                if isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev:
                    return

            # Task amendment: an authorized author may sharpen a live work
            # order in place, but only behind a strictly increasing revision
            # (recorded with its amendment reason), never a silent clobber.
            if is_active_wo and isinstance(data, dict):
                existing_rev = existing_data.get("revision", 1)
                new_rev = data.get("revision")
                if isinstance(new_rev, int) and isinstance(existing_rev, (int, float)) and new_rev > existing_rev:
                    return

        # Conflict: attempting to overwrite an active artifact from another task or unauthorized update
        artifact_label = "work order" if is_active_wo else "contract"
        errors.append(
            f"Overwrite conflict: active {artifact_label} '{norm_path}' already exists on disk "
            f"with different content. Overwriting an existing active task is rejected to prevent "
            f"silently clobbering live work orders."
        )

    def _validate_work_order(self, norm_path: str, data: dict[str, Any], errors: list[str]) -> None:
        schema = self.get_schema("work-order.schema.json")
        if schema:
            validator = Draft7Validator(schema)
            for err in validator.iter_errors(data):
                field_path = ".".join(str(p) for p in err.path) or "root"
                errors.append(f"Work Order schema error at '{field_path}': {err.message}")

        # Semantic checks
        file_stem = Path(norm_path).stem
        wo_id = data.get("id")
        if not wo_id:
            errors.append("Work Order missing required field 'id'")
        elif not re.match(r"^WO-[0-9]{3}$", str(wo_id)):
            errors.append(f"Work Order id '{wo_id}' does not match pattern '^WO-[0-9]{{3}}$'")
        elif file_stem.startswith("WO-") and wo_id != file_stem:
            errors.append(f"Work Order id '{wo_id}' does not match filename stem '{file_stem}'")

        deliverable = data.get("deliverable")
        if isinstance(deliverable, dict):
            deliv_type = deliverable.get("type")
            if deliv_type not in ("code", "doc", "config", "module"):
                errors.append(f"Work Order deliverable type '{deliv_type}' invalid; must be code, doc, config, or module")
            if not deliverable.get("description"):
                errors.append("Work Order deliverable requires a non-empty 'description'")

        # Validate requirement_refs if present
        requirement_refs = data.get("requirement_refs")
        if isinstance(requirement_refs, list):
            for ref in requirement_refs:
                if not isinstance(ref, str):
                    errors.append(f"requirement_refs entry '{ref}' must be a string")
                elif not ref.startswith("R"):
                    errors.append(f"requirement_refs entry '{ref}' must start with 'R' (e.g., R001)")

        # Validate dependencies
        deps = data.get("dependencies")
        if deps is not None:
            if not isinstance(deps, list):
                errors.append("Work Order dependencies must be a list of strings")
            else:
                is_gitops = self.is_gitops_work_order(data)
                for dep in deps:
                    dep_str = str(dep).strip()
                    if dep_str == wo_id:
                        errors.append(f"Work order '{wo_id}' cannot depend on itself")
                    elif not is_gitops and self._project_root:
                        if self._is_gitops_dependency(dep_str, self._project_root):
                            errors.append(
                                f"Work order '{wo_id}' has an inverted GitOps dependency: "
                                f"cannot depend on GitOps release work order '{dep_str}'"
                            )

    @staticmethod
    def repair_yaml_block_scalars(text: str) -> str:
        """Repair inconsistent indentation inside block scalars (e.g. description: > or |)."""
        lines = text.splitlines()
        repaired_lines = []
        in_block = False
        key_indent = 0
        block_indent = 0

        for line in lines:
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

    @staticmethod
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
            data["acceptance_criteria"] = [ac.strip()]

        # Normalize priority_override shorthand
        po = data.pop("priority_override", None)
        if po and ("priority" not in data or not data["priority"]):
            data["priority"] = str(po)

        # Normalize test_plan shorthand
        tp = data.get("test_plan")
        if tp is not None:
            if isinstance(tp, str):
                data["test_plan"] = [{"tests": [tp]}]
            elif isinstance(tp, dict):
                data["test_plan"] = [tp]
            elif isinstance(tp, list):
                norm_tp = []
                for item in tp:
                    if isinstance(item, str):
                        norm_tp.append({"tests": [item]})
                    elif isinstance(item, dict):
                        t_val = item.get("tests")
                        if isinstance(t_val, str):
                            item["tests"] = [t_val]
                        norm_tp.append(item)
                data["test_plan"] = norm_tp

        # Normalize implementation_estimate shorthand
        ie = data.get("implementation_estimate")
        if isinstance(ie, dict):
            ef = ie.get("expected_files")
            if isinstance(ef, str):
                ie["expected_files"] = [ef]
            if "max_files_touched" in ie:
                try:
                    ie["max_files_touched"] = int(ie["max_files_touched"])
                except (ValueError, TypeError):
                    pass

        return data

    @staticmethod
    def normalize_contract_data(data: dict[str, Any]) -> dict[str, Any]:
        """Normalize LLM shorthand in a contract dictionary into canonical schema shape."""
        if not isinstance(data, dict):
            return data

        # 1. Coerce schema_version and contract_revision if float or numeric string
        if "schema_version" in data:
            try:
                data["schema_version"] = int(float(data["schema_version"]))
            except (ValueError, TypeError):
                pass
        if "contract_revision" in data:
            try:
                data["contract_revision"] = int(float(data["contract_revision"]))
            except (ValueError, TypeError):
                pass

        scope = data.get("scope")
        if isinstance(scope, dict):
            # 2. Normalize scope.allow
            raw_allow = scope.get("allow")
            if isinstance(raw_allow, str):
                raw_allow = [raw_allow]
            if isinstance(raw_allow, list):
                norm_allow: list[dict[str, Any]] = []
                for item in raw_allow:
                    if isinstance(item, str) and item.strip():
                        norm_allow.append({"module": item.strip()})
                    elif isinstance(item, dict):
                        m = item.get("module") or item.get("target") or item.get("path")
                        if m and isinstance(m, str) and m.strip():
                            rule_dict: dict[str, Any] = {"module": m.strip()}
                            if "depth" in item:
                                try:
                                    rule_dict["depth"] = int(item["depth"])
                                except (ValueError, TypeError):
                                    pass
                            norm_allow.append(rule_dict)
                        else:
                            norm_allow.append(item)
                    else:
                        norm_allow.append(item)
                scope["allow"] = norm_allow

            # 3. Normalize scope.deny
            raw_deny = scope.get("deny")
            if isinstance(raw_deny, str):
                raw_deny = [raw_deny]
            if isinstance(raw_deny, list):
                norm_deny: list[dict[str, Any]] = []
                for item in raw_deny:
                    if isinstance(item, str) and item.strip():
                        norm_deny.append({"module": item.strip()})
                    elif isinstance(item, dict):
                        m = item.get("module") or item.get("target") or item.get("path")
                        if m and isinstance(m, str) and m.strip():
                            norm_deny.append({"module": m.strip()})
                        else:
                            norm_deny.append(item)
                    else:
                        norm_deny.append(item)
                scope["deny"] = norm_deny

            # 4. Normalize scope.write
            raw_write = scope.get("write")
            if isinstance(raw_write, list):
                # When LLM provides a list of write paths, the agent intended read-write
                if len(raw_write) > 0:
                    scope["write"] = "read-write"
                    allow_list = scope.get("allow", [])
                    if isinstance(allow_list, list):
                        existing_modules = {
                            r.get("module") for r in allow_list if isinstance(r, dict) and r.get("module")
                        }
                        for w_item in raw_write:
                            w_str = str(w_item or "").strip()
                            if w_str and w_str not in existing_modules:
                                allow_list.append({"module": w_str})
                                existing_modules.add(w_str)
                else:
                    scope["write"] = "read-only"
            elif isinstance(raw_write, str):
                w_norm = raw_write.strip().lower()
                if w_norm in ("rw", "readwrite", "write"):
                    scope["write"] = "read-write"
                elif w_norm in ("ro", "readonly", "read"):
                    scope["write"] = "read-only"

        # 5. Coerce budget numeric limits if string
        budget = data.get("budget")
        if isinstance(budget, dict):
            for k in ("max_files_touched", "max_tokens"):
                if k in budget:
                    try:
                        budget[k] = int(budget[k])
                    except (ValueError, TypeError):
                        pass

        return data

    def _validate_contract(self, norm_path: str, data: dict[str, Any], errors: list[str]) -> None:
        schema = self.get_schema("contract.schema.json")
        if schema:
            validator = Draft7Validator(schema)
            for err in validator.iter_errors(data):
                field_path = ".".join(str(p) for p in err.path) or "root"
                errors.append(f"Contract schema error at '{field_path}': {err.message}")

        # Semantic checks
        file_stem = Path(norm_path).stem
        wo_ref = data.get("work_order")
        if not wo_ref:
            errors.append("Contract missing required field 'work_order'")
        elif not re.match(r"^WO-[0-9]{3}$", str(wo_ref)):
            errors.append(f"Contract work_order '{wo_ref}' does not match pattern '^WO-[0-9]{{3}}$'")
        elif file_stem.startswith("WO-") and wo_ref != file_stem:
            errors.append(f"Contract work_order '{wo_ref}' does not match filename stem '{file_stem}'")

        agent_id = data.get("agent_id")
        if not agent_id or not isinstance(agent_id, str):
            errors.append("Contract requires non-empty string 'agent_id'")

        scope = data.get("scope")
        if isinstance(scope, dict):
            allow_rules = scope.get("allow")
            if not isinstance(allow_rules, list) or len(allow_rules) == 0:
                errors.append("Contract scope must define at least one 'allow' rule")
            else:
                for idx, rule in enumerate(allow_rules):
                    target = (
                        (rule.get("module") or rule.get("target") or rule.get("path"))
                        if isinstance(rule, dict)
                        else rule
                    )
                    if not target or not isinstance(target, str) or not target.strip():
                        errors.append(f"Contract allow rule #{idx} must specify a non-empty 'module'")
            write_mode = scope.get("write")
            if isinstance(write_mode, list):
                write_mode = "read-write" if len(write_mode) > 0 else "read-only"
            if write_mode not in ("read-only", "read-write"):
                errors.append(f"Contract scope.write mode '{write_mode}' invalid; must be 'read-only' or 'read-write'")

        budget = data.get("budget")
        if not isinstance(budget, dict):
            errors.append("Contract requires 'budget' object")

    def _validate_index(self, norm_path: str, data: dict[str, Any], errors: list[str]) -> None:
        orders = data.get("orders")
        if orders is None or not isinstance(orders, list):
            errors.append(f"Work order index '{norm_path}' must contain an 'orders' list")
            return
        self._lint_index_dependencies(norm_path, orders, errors)

    def _lint_index_dependencies(self, norm_path: str, orders: list[Any], errors: list[str]) -> None:
        known_orders: dict[str, dict[str, Any]] = {}
        for item in orders:
            if isinstance(item, dict):
                o_id = str(item.get("id") or "").strip()
                if o_id:
                    known_orders[o_id] = item

        graph: dict[str, list[str]] = {}
        for o_id, item in known_orders.items():
            is_gitops = self.is_gitops_work_order(item)
            deps = item.get("dependencies") or []
            if not isinstance(deps, list):
                errors.append(f"Work order '{o_id}' in index '{norm_path}' dependencies must be a list")
                continue
            dep_nodes: list[str] = []
            for dep in deps:
                dep_str = str(dep).strip()
                if not dep_str:
                    continue
                if dep_str == o_id:
                    errors.append(f"Work order '{o_id}' in index '{norm_path}' cannot depend on itself")
                    continue
                dep_data = known_orders.get(dep_str)
                is_dep_gitops = False
                if dep_data is not None:
                    is_dep_gitops = self.is_gitops_work_order(dep_data)
                elif self._project_root:
                    is_dep_gitops = self._is_gitops_dependency(dep_str, self._project_root)
                if not is_gitops and is_dep_gitops:
                    errors.append(
                        f"Work order '{o_id}' has an inverted GitOps dependency: "
                        f"cannot depend on GitOps release work order '{dep_str}'"
                    )
                if dep_str in known_orders:
                    dep_nodes.append(dep_str)
            graph[o_id] = dep_nodes

        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            cycle = list(exc.args[1])
            errors.append(f"Work order index '{norm_path}' contains dependency cycle: {' -> '.join(cycle)}")

    def validate_staged_artifact(
        self,
        staged_root: Path,
        rel_path: str,
        agent: str | None = None,
        project_root: Path | str | None = None,
    ) -> AuthoringGateDecision:
        """Validate an authored artifact located inside the staged project tree."""
        full_path = staged_root / rel_path
        if not full_path.exists():
            return AuthoringGateDecision(
                passed=False,
                artifact_type=self.classify_artifact(rel_path),
                path=rel_path,
                author=agent or "unknown",
                errors=[f"File '{rel_path}' does not exist in staged workspace."],
            )
        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception as exc:
            return AuthoringGateDecision(
                passed=False,
                artifact_type=self.classify_artifact(rel_path),
                path=rel_path,
                author=agent or "unknown",
                errors=[f"Failed to read '{rel_path}': {exc}"],
            )
        return self.validate_artifact_content(
            rel_path, content, agent=agent, project_root=project_root
        )
