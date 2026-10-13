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
        # 2. Check current working directory / schemas
        candidate = Path.cwd() / "schemas"
        if candidate.exists() and (candidate / "contract.schema.json").exists():
            return candidate
        # Fallback to repo candidate
        return candidate

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

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
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
        normalized_content: str | None = None
        if artifact_type == "work_order":
            self._validate_work_order(norm_path, data, errors)
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
        if not (is_active_wo or is_contract):
            return

        # Check candidate locations in project root
        candidates = [project_root / norm_path]
        if not norm_path.startswith(".sync/"):
            candidates.append(project_root / ".sync" / norm_path)

        existing_file = next((p for p in candidates if p.is_file()), None)
        if existing_file is None:
            return

        # If already authored by THIS session, allow updating it
        if norm_path in self._session_authored_paths:
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

        # Scaffolding or repair artifacts: an authorized author (Architect/CEO)
        # may supersede synthesized artifacts or amend contracts with incremented revision.
        # Workers never reach this check (role gate runs first).
        if str(author or "").lower() in self.AUTHORIZED_AUTHORS and isinstance(existing_data, dict):
            existing_synth = str(existing_data.get("synthesized_by", "")).lower()
            new_synth = str(data.get("synthesized_by", "") if isinstance(data, dict) else "").lower()
            if existing_synth in {"bootstrap", "recovery-repair"} or new_synth in {"bootstrap", "recovery-repair"}:
                return
            if is_contract and isinstance(data, dict):
                existing_rev = existing_data.get("contract_revision", 1)
                new_rev = data.get("contract_revision")
                if isinstance(new_rev, int) and isinstance(existing_rev, int) and new_rev > existing_rev:
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
        if "orders" not in data or not isinstance(data["orders"], list):
            errors.append(f"Work order index '{norm_path}' must contain an 'orders' list")

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
