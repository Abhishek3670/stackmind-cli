"""The P1 tool gateway: all tools cross policy, contract, and journal boundaries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .boundary import RuntimeBoundary
from .contract import AgentContract
from .identity import AuthorizationPolicy
from .interpreter_denylist import check_command
from .operations import OperationRequest, OperationType
from .sandbox import ProcessSandbox
from .workspace import ScratchWorkspace


class ToolGateway:
    def __init__(self, workspace: ScratchWorkspace, boundary: RuntimeBoundary,
                 contract: AgentContract, policy: AuthorizationPolicy, session_id: str,
                 attempt_id: str, actor_id: str, provider_id: str,
                 graph_query: Callable[[str], Any] | None = None,
                 sandbox: ProcessSandbox | None = None) -> None:
        self.workspace, self.boundary = workspace, boundary
        self.contract, self.policy = contract, policy
        self.session_id, self.attempt_id = session_id, attempt_id
        self.actor_id, self.provider_id = actor_id, provider_id
        self.graph_query = graph_query
        self.sandbox = sandbox or ProcessSandbox(workspace)

    def _request(self, operation_type: OperationType, target: str) -> OperationRequest:
        return OperationRequest(operation_type, target, self.session_id, self.attempt_id,
                                self.actor_id, self.provider_id)

    def _authorize(self, operation_type: OperationType, target: str):
        return self.boundary.submit(self._request(operation_type, target), self.contract, self.policy)

    def read_file(self, target: str) -> str:
        record = self._authorize(OperationType.READ_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)
        value = self.workspace.path_for(target).read_text(encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "read")
        return value

    def write_file(self, target: str, content: str) -> None:
        record = self._authorize(OperationType.WRITE_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)
        path = self.workspace.path_for(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "written")

    def run_command(self, command: Sequence[str]):
        record = self._authorize(OperationType.RUN_COMMAND, "workspace/command")
        if not record.authorized:
            raise PermissionError(record.reason)
        # Hard interpreter denylist (Phase 2): shells and string-code
        # interpreters are denied at the agent-facing boundary regardless of
        # contract grants. Trusted platform-internal callers (canary verifier,
        # evidence tracer) construct their own ProcessSandbox and are not
        # subject to this gate.
        denial_reason = check_command(command)
        if denial_reason is not None:
            raise PermissionError(denial_reason)
        result = self.sandbox.run(command)
        self.boundary.journal.complete(record.request.operation_id, result)
        return result

    def query_graph(self, query: str) -> Any:
        record = self._authorize(OperationType.QUERY_GRAPH, "graph/query")
        if not record.authorized:
            raise PermissionError(record.reason)
        if self.graph_query is None:
            raise RuntimeError("No graph query provider configured")
        result = self.graph_query(query)
        self.boundary.journal.complete(record.request.operation_id, "queried")
        return result
