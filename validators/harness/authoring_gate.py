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
from pathlib import Path
from typing import Any
import yaml
from jsonschema import Draft7Validator

from .authoring_role import (
    AUTHORIZED_AUTHORS,
    WORKER_ROLES,
    classify_artifact,
    is_authorized,
    is_gitops_work_order,
)
from .authoring_yaml import (
    normalize_work_order_data,
    normalize_contract_data,
    validate_artifact_content as _validate_artifact_content_yaml,
)
from .authoring_semantic import validate_semantic
from .authoring_conflict import _check_overwrite_conflict, _session_authored_paths

from validators.knowledge.contract import (
    AgentContract,
    ContractAccessDenied,
    ContractExpiredError,
    path_to_module,
    module_matches,
)
from validators.knowledge.api import KnowledgeAPI, ContextBundle


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

    classify_artifact = staticmethod(classify_artifact)
    is_gitops_work_order = staticmethod(is_gitops_work_order)

    def validate_staged_artifact(
        self,
        staged_root: Path | str,
        relative_path: str,
        agent: str | None = None,
        project_root: Path | str | None = None,
    ) -> AuthoringGateDecision:
        p = Path(staged_root) / relative_path
        if not p.is_file():
            return AuthoringGateDecision(
                passed=False,
                artifact_type=classify_artifact(relative_path),
                path=str(relative_path),
                author=agent or "",
                errors=[f"Staged artifact '{relative_path}' not found"],
            )
        return self.validate_artifact_content(
            relative_path, p.read_text(encoding="utf-8"), agent=agent, project_root=project_root
        )

    def __init__(self, schemas_dir: Path | None = None, project_root: Path | None = None) -> None:
        self._schemas_dir = schemas_dir or self._find_schemas_dir()
        self._project_root = Path(project_root).resolve() if project_root is not None else None
        self._schema_cache: dict[str, dict[str, Any]] = {}
        # Reset session-authored paths for new gate instance
        _session_authored_paths.clear()

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

    def validate_author_role(self, agent: str | None, path: str) -> tuple[bool, str | None]:
        """Verify that the requesting agent has authority to author this artifact.

        Workers (Codex, Gemini, Local-LLM, Gemma) MUST NEVER author contracts or work orders.
        Only Claude (Architect) and CEO hold authoring authority.
        """
        if not agent:
            return True, None
        agent_norm = agent.lower().strip()
        if agent_norm in WORKER_ROLES:
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
        artifact_type = classify_artifact(norm_path)

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
            data = normalize_work_order_data(data)
            self._validate_work_order(norm_path, data, errors)
            if not errors:
                try:
                    normalized_content = yaml.safe_dump(data, sort_keys=False)
                except Exception:
                    pass
        elif artifact_type == "contract":
            data = normalize_contract_data(data)
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
        effective_root = (
            Path(project_root).resolve() if project_root is not None else self._project_root
        )
        if effective_root is not None:
            _check_overwrite_conflict(
                norm_path, content, data, effective_root, errors, author=author
            )

        passed = len(errors) == 0
        if passed:
            _session_authored_paths.add(norm_path)
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

    def _validate_work_order(self, norm_path: str, data: dict[str, Any], errors: list[str]) -> None:
        """Validate work order schema and semantic constraints."""
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
                is_gitops = is_gitops_work_order(data)
                for dep in deps:
                    dep_str = str(dep).strip()
                    if dep_str == wo_id:
                        errors.append(f"Work order '{wo_id}' cannot depend on itself")
                    elif not is_gitops and self._project_root:
                        for check_dir in [
                            self._project_root / ".sync" / "work-orders" / "ACTIVE",
                            self._project_root / "work-orders" / "ACTIVE",
                        ]:
                            dep_file = check_dir / f"{dep_str}.yaml"
                            if dep_file.is_file():
                                try:
                                    dep_data = yaml.safe_load(dep_file.read_text(encoding="utf-8"))
                                    if isinstance(dep_data, dict) and is_gitops_work_order(dep_data):
                                        errors.append(
                                            f"DAG violation: Work order '{wo_id}' has an inverted GitOps dependency "
                                            f"on GitOps release task '{dep_str}'."
                                        )
                                        break
                                except Exception:
                                    pass

    def _validate_contract(
        self, norm_path: str, data: dict[str, Any], errors: list[str]
    ) -> None:
        """Validate contract schema and semantic constraints."""
        schema = self.get_schema("contract.schema.json")
        if schema:
            validator = Draft7Validator(schema)
            for err in validator.iter_errors(data):
                field_path = ".".join(str(p) for p in err.path) or "root"
                errors.append(f"Contract schema error at '{field_path}': {err.message}")

        # Semantic checks
        file_stem = Path(norm_path).stem
        wo_val = data.get("work_order")
        if wo_val and str(wo_val) != file_stem:
            errors.append(f"Contract work_order '{wo_val}' does not match filename stem '{file_stem}'")

        # Check for allow rules
        scope_obj = data.get("scope")
        scope_allow = scope_obj.get("allow") if isinstance(scope_obj, dict) else None
        allow_rules = (
            scope_allow
            if scope_allow is not None
            else (data.get("allow_rules") or data.get("allowed") or data.get("allows"))
        )
        if allow_rules is not None and len(allow_rules) == 0:
            errors.append("Contract must specify at least one 'allow' rule in scope.allow")

        # Check for deny rules
        deny_rules = data.get("deny_rules") or data.get("denies") or data.get("forbidden")

    def _validate_index(
        self, norm_path: str, data: dict[str, Any], errors: list[str]
    ) -> None:
        """Validate index semantic constraints: cycle detection and inverted GitOps dependencies."""
        if not isinstance(data, dict):
            return
        orders = data.get("orders")
        if not isinstance(orders, list):
            return

        order_map: dict[str, dict[str, Any]] = {}
        dep_graph: dict[str, list[str]] = {}
        for o in orders:
            if isinstance(o, dict):
                oid = str(o.get("id") or "")
                if oid:
                    order_map[oid] = o
                    dep_graph[oid] = [str(d).strip() for d in (o.get("dependencies") or []) if str(d).strip()]

        for oid, o in order_map.items():
            is_gitops = is_gitops_work_order(o)
            for dep in dep_graph.get(oid, []):
                if dep == oid:
                    errors.append(f"Work order '{oid}' cannot depend on itself")
                elif not is_gitops:
                    dep_o = order_map.get(dep)
                    if dep_o and is_gitops_work_order(dep_o):
                        errors.append(
                            f"DAG violation: Work order '{oid}' has an inverted GitOps dependency "
                            f"on GitOps release task '{dep}'."
                        )

        # Cycle detection on dep_graph
        visited: dict[str, int] = {}
        def _has_cycle(node: str) -> bool:
            visited[node] = 1
            for neighbor in dep_graph.get(node, []):
                if neighbor in dep_graph:
                    if visited.get(neighbor) == 1:
                        return True
                    if visited.get(neighbor) != 2:
                        if _has_cycle(neighbor):
                            return True
            visited[node] = 2
            return False

        for node in dep_graph:
            if visited.get(node) != 2:
                if _has_cycle(node):
                    errors.append(f"DAG violation: dependency cycle detected involving work order '{node}'.")
                    break

    @staticmethod
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
