"""The narrowly-scoped MCP façade for governed StackMind operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contract import AgentContract
from ..daemon import SessionManager
from ..tools import ToolGateway
from .modes import OperatingModeTracker

_STRING = {"type": "string"}


class GovernedToolRegistry:
    """MCP tools backed exclusively by the StackMind gateway and scratch workspace."""

    def __init__(
        self,
        gateway: ToolGateway,
        manager: SessionManager,
        session_id: str,
        contract: AgentContract,
        modes: OperatingModeTracker | None = None,
        context_provider: Callable[[str], Any] | None = None,
        review_submitter: Callable[[Path], Any] | None = None,
    ) -> None:
        self.gateway, self.manager, self.session_id = gateway, manager, session_id
        self.contract, self.modes = contract, modes or OperatingModeTracker()
        self.context_provider, self.review_submitter = context_provider, review_submitter
        self.modes.start(session_id)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            self._definition(
                "stackmind.read_file",
                "Read a contract-authorized scratch file",
                {"path": _STRING},
                ["path"],
            ),
            self._definition(
                "stackmind.write_file",
                "Write a contract-authorized scratch file",
                {"path": _STRING, "content": _STRING},
                ["path", "content"],
            ),
            self._definition(
                "stackmind.run_command",
                "Run a sandboxed command in the scratch workspace",
                {"command": {"type": "array", "items": _STRING}},
                ["command"],
            ),
            self._definition(
                "stackmind.query_graph",
                "Query the governed knowledge graph",
                {"query": _STRING},
                ["query"],
            ),
            self._definition(
                "stackmind.get_context",
                "Assemble bounded governed task context",
                {"query": _STRING},
                ["query"],
            ),
            self._definition(
                "stackmind.get_contract", "Inspect the active contract and remaining budget", {}, []
            ),
            self._definition(
                "stackmind.submit_for_review",
                "Package verified scratch changes for QA review",
                {},
                [],
            ),
        ]

    @staticmethod
    def _definition(
        name: str, description: str, properties: dict[str, Any], required: list[str]
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        arguments = arguments or {}
        if not isinstance(arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        self.manager.begin_operation(self.session_id, name)
        operation_id = self.manager._sessions[self.session_id]["active_operation"]
        try:
            result = self._call(name, arguments)
        except Exception as error:
            self.manager.complete_operation(self.session_id, operation_id, {"error": str(error)})
            raise
        self.manager.complete_operation(self.session_id, operation_id, {"tool": name})
        self.modes.record_governed(self.session_id)
        return result

    def _call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if name == "stackmind.read_file":
            return self.gateway.read_file(self._string(arguments, "path"))
        if name == "stackmind.write_file":
            self.gateway.write_file(
                self._string(arguments, "path"), self._string(arguments, "content")
            )
            return {"written": True}
        if name == "stackmind.run_command":
            command = arguments.get("command")
            if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
                raise ValueError("command must be a string array")
            result = self.gateway.run_command(command)
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        if name == "stackmind.query_graph":
            return self.gateway.query_graph(self._string(arguments, "query"))
        if name == "stackmind.get_context":
            if self.context_provider is None:
                raise RuntimeError("No context provider configured")
            return self.context_provider(self._string(arguments, "query"))
        if name == "stackmind.get_contract":
            state = self.modes.state_for(self.session_id)
            return {
                "agent_id": self.contract.agent_id,
                "work_order": self.contract.work_order,
                "allow": list(self.contract.allow),
                "deny": list(self.contract.deny),
                "write_mode": self.contract.write_mode,
                "budget": dict(self.contract.budget),
                "learning_eligible": state.learning_eligible,
                "mode": state.mode.value,
            }
        if name == "stackmind.submit_for_review":
            self.gateway.workspace.verify()
            change_set = self.gateway.workspace.change_set()
            submitted = self.review_submitter(change_set) if self.review_submitter else None
            return {"workspace": str(change_set), "submitted": submitted, "verified": True}
        raise ValueError("Unknown MCP tool")

    @staticmethod
    def _string(arguments: Mapping[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value
