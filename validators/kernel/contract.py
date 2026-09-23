"""Tolerant contract ingestion with a strict, immutable runtime representation."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


class ContractNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class AgentContract:
    agent_id: str
    work_order: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    write_mode: str = "read-only"
    version: str = "1"
    budget: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def freeze(self) -> "AgentContract":
        return AgentContract(
            self.agent_id,
            self.work_order,
            tuple(self.allow),
            tuple(self.deny),
            self.write_mode,
            self.version,
            MappingProxyType(dict(self.budget)),
        )


class ContractNormalizer:
    """Accept common LLM-shaped aliases, then reject incomplete canonical contracts."""

    @staticmethod
    def normalize(raw: Mapping[str, Any]) -> AgentContract:
        if not isinstance(raw, Mapping):
            raise ContractNormalizationError("Contract must be an object")
        raw = raw.get("contract", raw)
        if not isinstance(raw, Mapping):
            raise ContractNormalizationError("Wrapped contract must be an object")
        scope = raw.get("scope", {})
        if not isinstance(scope, Mapping):
            raise ContractNormalizationError("scope must be an object")

        def rules(value: Any) -> tuple[str, ...]:
            if value is None:
                return ()
            if isinstance(value, (str, Mapping)):
                value = [value]
            if not isinstance(value, list):
                raise ContractNormalizationError("scope rules must be a list")
            values = []
            for item in value:
                target = (
                    item.get("module") or item.get("target") if isinstance(item, Mapping) else item
                )
                if not isinstance(target, str) or not target.strip():
                    raise ContractNormalizationError("Each scope rule must name a target")
                values.append(target.strip().replace("\\", "/"))
            return tuple(values)

        agent_id = raw.get("agent_id") or raw.get("agent") or raw.get("agentId")
        work_order = raw.get("work_order") or raw.get("workOrder") or raw.get("wo")
        allow = rules(scope.get("allow", scope.get("allowed")))
        deny = rules(scope.get("deny", scope.get("denied")))
        write_mode = scope.get("write", raw.get("write_mode", "read-only"))
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(work_order, str)
            or not work_order
        ):
            raise ContractNormalizationError("agent_id and work_order are required")
        if not allow or write_mode not in {"read-only", "read-write"}:
            raise ContractNormalizationError("Contract requires allow rules and a valid write mode")
        budget = raw.get("budget", {})
        if not isinstance(budget, Mapping):
            raise ContractNormalizationError("budget must be an object")
        return AgentContract(
            agent_id,
            work_order,
            allow,
            deny,
            write_mode,
            str(raw.get("version", raw.get("contract_version", "1"))),
            MappingProxyType(dict(budget)),
        )


class ContractEvaluator:
    """Fail-closed target and operation authorization evaluator."""

    _WRITE_OPERATIONS = {"write_file", "delete_file", "run_command"}

    @staticmethod
    def _matches(target: str, rule: str) -> bool:
        normalized = target.replace("\\", "/")
        return (
            fnmatch.fnmatch(normalized, rule)
            or normalized == rule
            or normalized.startswith(rule.rstrip("/") + "/")
        )

    def authorize(
        self, contract: AgentContract, operation_type: str, target: str
    ) -> tuple[bool, str]:
        if not target:
            return False, "missing target"
        if target.startswith(("/", "\\")) or ".." in target.replace("\\", "/").split("/"):
            return False, "target path traversal is forbidden"
        if operation_type in self._WRITE_OPERATIONS and contract.write_mode != "read-write":
            return False, "contract is read-only"
        if any(self._matches(target, rule) for rule in contract.deny):
            return False, "target is explicitly denied"
        if not any(self._matches(target, rule) for rule in contract.allow):
            return False, "target is outside allowed scope"
        return True, "authorized"
