"""Execution Backend Abstraction Layer for StackMind Phase 7.

Defines the ExecutionBackend protocol, standard adapter interface,
Agent and Model concrete backend implementations, and the BackendRegistry.
Enforces the zero credential leakage invariant across all operations,
RPC responses, and serialized states.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "token",
    "password",
    "authorization",
    "access_token",
    "private_key",
}


def sanitize_secrets(value: Any) -> Any:
    """Recursively strip or mask credentials from any data structure."""
    if isinstance(value, dict):
        sanitized = {}
        for k, v in value.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                continue
            sanitized[k] = sanitize_secrets(v)
        return sanitized
    if isinstance(value, list):
        return [sanitize_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_secrets(item) for item in value)
    return value


class BackendError(Exception):
    """Base exception for execution backend errors."""


class BackendExecutionError(BackendError):
    """Raised when an operation fails during execution on a backend."""


class BackendTimeoutError(BackendExecutionError):
    """Raised when a backend operation times out."""


class BackendUnavailableError(BackendError):
    """Raised when a backend is unreachable, unconfigured, or offline."""


class BackendPolicyError(BackendError):
    """Raised when a backend denies an operation due to policy or contract."""


@runtime_checkable
class ExecutionBackend(Protocol):
    """Protocol governing all StackMind execution backends."""

    backend_id: str
    backend_type: str  # "agent" | "model"
    model: str | None
    status: str  # "available" | "unavailable" | "not configured"
    capabilities: list[str]
    credential_ref: str | None

    def start_operation(
        self,
        task: Any,
        context: Any = None,
        *,
        operation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Start a new operation on this backend, returning the operation_id."""

    def send_work(
        self,
        operation_id: str,
        message: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send work or a prompt to an active operation."""

    def stream_events(
        self,
        operation_id: str,
    ) -> Iterator[dict[str, Any]]:
        """Yield events emitted by the operation."""

    def request_approval(
        self,
        operation_id: str,
        prompt: str,
        *,
        options: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Request operator approval from this backend."""

    def cancel(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        """Cancel an in-flight operation on this backend."""

    def inspect(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        """Inspect operation state and metadata without exposing credentials."""

    def report_result(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        """Report completion or failure outcome for an operation."""

    def as_dict(self) -> dict[str, Any]:
        """Return the serializable public description of this backend."""


class BaseExecutionBackend:
    """Base class providing common operation tracking and event recording."""

    def __init__(
        self,
        backend_id: str,
        backend_type: str,
        model: str | None = None,
        *,
        endpoint: str | None = None,
        credential_ref: str | None = None,
        timeout: float = 30.0,
        budget_policy: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        status: str = "available",
    ) -> None:
        self.backend_id = backend_id
        self.backend_type = backend_type
        self.model = model
        self.endpoint = endpoint
        self.credential_ref = credential_ref
        self.timeout = timeout
        self.budget_policy = budget_policy or {}
        self.capabilities = list(capabilities or [])
        self._status = status
        self._operations: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value

    def as_dict(self) -> dict[str, Any]:
        """Public description for backend.list and discovery; zero credential leakage."""
        data: dict[str, Any] = {
            "id": self.backend_id,
            "type": self.backend_type,
            "status": self.status,
            "capabilities": list(self.capabilities),
        }
        if self.model:
            data["model"] = self.model
        if self.endpoint:
            data["endpoint"] = self.endpoint
        if self.credential_ref:
            data["credentialRef"] = self.credential_ref
        if self.budget_policy:
            data["budgetPolicy"] = sanitize_secrets(self.budget_policy)
        return data

    def start_operation(
        self,
        task: Any,
        context: Any = None,
        *,
        operation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        op_id = operation_id or f"op-{uuid4()}"
        now = _now()
        task_id = getattr(task, "identifier", str(task) if task else None)
        record = {
            "operation_id": op_id,
            "backend_id": self.backend_id,
            "backend_type": self.backend_type,
            "model": self.model,
            "task_id": task_id,
            "status": "RUNNING",
            "created_at": now,
            "updated_at": now,
            "approvals": [],
            "result": None,
            "error": None,
        }
        self._operations[op_id] = record
        self._events[op_id] = [
            {"event": "operation.started", "operation_id": op_id, "timestamp": now}
        ]
        return op_id

    def send_work(
        self,
        operation_id: str,
        message: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        op = self._operations[operation_id]
        if op["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"Operation '{operation_id}' is terminal ({op['status']})")
        now = _now()
        op["updated_at"] = now
        msg_summary = str(message)[:200]
        event = {
            "event": "work.sent",
            "operation_id": operation_id,
            "message": msg_summary,
            "timestamp": now,
        }
        self._events[operation_id].append(event)
        return {"operation_id": operation_id, "status": op["status"]}

    def stream_events(
        self,
        operation_id: str,
    ) -> Iterator[dict[str, Any]]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        events = list(self._events.get(operation_id, []))
        for event in events:
            yield dict(event)

    def request_approval(
        self,
        operation_id: str,
        prompt: str,
        *,
        options: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        op = self._operations[operation_id]
        approval_id = f"appr-{uuid4()}"
        now = _now()
        record = {
            "approval_id": approval_id,
            "operation_id": operation_id,
            "prompt": prompt,
            "options": options or ["approve", "reject"],
            "status": "PENDING",
            "created_at": now,
        }
        op.setdefault("approvals", []).append(record)
        self._events[operation_id].append(
            {
                "event": "approval.requested",
                "approval_id": approval_id,
                "operation_id": operation_id,
                "prompt": prompt,
                "timestamp": now,
            }
        )
        return record

    def cancel(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        op = self._operations[operation_id]
        now = _now()
        op["status"] = "CANCELLED"
        op["updated_at"] = now
        self._events[operation_id].append(
            {"event": "operation.cancelled", "operation_id": operation_id, "timestamp": now}
        )
        return sanitize_secrets(dict(op))

    def inspect(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        return sanitize_secrets(dict(self._operations[operation_id]))

    def report_result(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        if operation_id not in self._operations:
            raise KeyError(f"Operation '{operation_id}' not found")
        op = self._operations[operation_id]
        return sanitize_secrets({
            "operation_id": operation_id,
            "status": op.get("status"),
            "result": op.get("result"),
            "error": op.get("error"),
        })


class AgentExecutionBackend(BaseExecutionBackend):
    """Agent Backend adapter wrapping local runner or agent CLI."""

    provider_name: str
    model_name: str

    def __init__(
        self,
        backend_id: str = "echo-agent",
        model: str = "stackmind-echo-v1",
        *,
        default_release_target: str | None = None,
        should_fail: bool = False,
        failure_reason: str = "Simulated agent backend failure",
        simulate_timeout: bool = False,
        timeout: float = 30.0,
        credential_ref: str | None = None,
        budget_policy: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        caps = capabilities or ["agent", "tools", "streaming", "cancellation", "approval"]
        super().__init__(
            backend_id=backend_id,
            backend_type="agent",
            model=model,
            credential_ref=credential_ref,
            timeout=timeout,
            budget_policy=budget_policy,
            capabilities=caps,
            status="available",
        )
        self.provider_name = backend_id
        self.model_name = model
        self.default_release_target = default_release_target
        self.should_fail = should_fail
        self.failure_reason = failure_reason
        self.simulate_timeout = simulate_timeout

    def complete(self, request: Any) -> Any:
        """Satisfies LLMProvider protocol for AgentRunner integration."""
        from validators.harness.runner import CompletionRecord

        if self.simulate_timeout:
            raise BackendTimeoutError(
                f"Agent backend '{self.backend_id}' timed out after {self.timeout}s"
            )
        if self.should_fail or self.status == "unavailable":
            raise BackendExecutionError(
                f"Agent backend '{self.backend_id}' error: {self.failure_reason}"
            )

        task = request.task
        release_target = self.default_release_target if task.work_order_id else None
        status = "completed"
        blockers: list[str] = []
        if task.work_order_id and not release_target:
            status = "deferred"
            blockers = ["release_target required for work-order completion"]

        payload = {
            "status": status,
            "summary": f"Processed {task.identifier}: {task.title}",
            "report_markdown": (
                f"Processed `{task.identifier}` as `{status}` via `{self.backend_id}`.\n\n"
                f"Knowledge revision: {getattr(request.context, 'revision', 'unknown')}\n"
                f"External evidence used: {len(getattr(request.retrieval, 'evidence', []))}"
            ),
            "blockers": blockers,
            "modified_files": (
                [task.deliverable_path] if task.deliverable_path else []
            ),
            "retrieval_queries": [request.retrieval.query] if getattr(request.retrieval, "query", None) else [],
            "uncertainty": [],
            "commands": [],
        }
        if release_target:
            payload["release_target"] = release_target

        return CompletionRecord(
            provider=self.backend_id,
            model=self.model or "default",
            payload=payload,
        )


EchoAgentBackend = AgentExecutionBackend


class ModelExecutionBackend(BaseExecutionBackend):
    """Model Backend adapter wrapping local LLM endpoints (e.g. Ollama, vLLM)."""

    provider_name: str
    model_name: str

    def __init__(
        self,
        backend_id: str = "ollama",
        model: str = "llama3",
        *,
        endpoint: str | None = "http://localhost:11434",
        credential_ref: str | None = None,
        timeout: float = 30.0,
        budget_policy: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        available: bool = True,
        should_fail: bool = False,
        failure_reason: str = "Simulated model backend failure",
        simulate_timeout: bool = False,
        default_release_target: str | None = None,
    ) -> None:
        caps = capabilities or ["model", "completion", "streaming", "cancellation", "approval"]
        status = "available" if (available and endpoint) else ("not configured" if not endpoint else "unavailable")
        super().__init__(
            backend_id=backend_id,
            backend_type="model",
            model=model,
            endpoint=endpoint,
            credential_ref=credential_ref,
            timeout=timeout,
            budget_policy=budget_policy,
            capabilities=caps,
            status=status,
        )
        self.provider_name = backend_id
        self.model_name = model
        self.available = available
        self.should_fail = should_fail
        self.failure_reason = failure_reason
        self.simulate_timeout = simulate_timeout
        self.default_release_target = default_release_target

    def complete(self, request: Any) -> Any:
        """Satisfies LLMProvider protocol for AgentRunner integration."""
        from validators.harness.runner import CompletionRecord

        if self.simulate_timeout:
            raise BackendTimeoutError(
                f"Model backend '{self.backend_id}' timed out after {self.timeout}s"
            )
        if self.should_fail or self.status == "unavailable":
            raise BackendExecutionError(
                f"Model backend '{self.backend_id}' error: {self.failure_reason}"
            )
        if self.status == "not configured":
            raise BackendUnavailableError(
                f"Model backend '{self.backend_id}' is not configured"
            )

        task = request.task
        release_target = self.default_release_target if task.work_order_id else None
        status = "completed"
        blockers: list[str] = []
        if task.work_order_id and not release_target:
            status = "deferred"
            blockers = ["release_target required for work-order completion"]

        payload = {
            "status": status,
            "summary": f"Processed {task.identifier}: {task.title} (model: {self.model})",
            "report_markdown": (
                f"Processed `{task.identifier}` via model `{self.model}` ({self.backend_id}).\n\n"
                f"Knowledge revision: {getattr(request.context, 'revision', 'unknown')}\n"
            ),
            "blockers": blockers,
            "modified_files": (
                [task.deliverable_path] if task.deliverable_path else []
            ),
            "retrieval_queries": [request.retrieval.query] if getattr(request.retrieval, "query", None) else [],
            "uncertainty": [],
            "commands": [],
        }
        if release_target:
            payload["release_target"] = release_target

        return CompletionRecord(
            provider=self.backend_id,
            model=self.model or "default",
            payload=payload,
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=25,
        )


OllamaBackend = ModelExecutionBackend


class BackendRegistry:
    """Registry managing execution backend instances, capability discovery, and health."""

    def __init__(self) -> None:
        self._backends: dict[str, ExecutionBackend] = {}

    def register(self, backend: ExecutionBackend) -> None:
        if not hasattr(backend, "backend_id") or not backend.backend_id:
            raise ValueError("backend must have a non-empty backend_id")
        self._backends[backend.backend_id] = backend

    def unregister(self, backend_id: str) -> None:
        self._backends.pop(backend_id, None)

    def get(self, backend_id: str) -> ExecutionBackend:
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise KeyError(f"Execution backend '{backend_id}' not found") from exc

    def has(self, backend_id: str) -> bool:
        return backend_id in self._backends

    def __contains__(self, backend_id: str) -> bool:
        return backend_id in self._backends

    def list_backends(self) -> list[dict[str, Any]]:
        """Return public list of registered backends with zero credential exposure."""
        result = []
        for backend in self._backends.values():
            if hasattr(backend, "as_dict"):
                result.append(backend.as_dict())
            else:
                entry = {
                    "id": backend.backend_id,
                    "type": backend.backend_type,
                    "status": backend.status,
                    "capabilities": list(backend.capabilities),
                }
                if backend.model:
                    entry["model"] = backend.model
                if getattr(backend, "credential_ref", None):
                    entry["credentialRef"] = backend.credential_ref
                result.append(entry)
        return result


_DEFAULT_REGISTRY: BackendRegistry | None = None


def get_default_registry() -> BackendRegistry:
    """Return the global default BackendRegistry, initializing standard adapters."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = BackendRegistry()
        reg.register(AgentExecutionBackend(backend_id="echo-agent", model="stackmind-echo-v1"))
        reg.register(ModelExecutionBackend(backend_id="mock-model", model="mock-llama3", available=True))
        reg.register(ModelExecutionBackend(backend_id="ollama", model="llama3", endpoint="http://localhost:11434", available=False))
        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY


def reset_default_registry() -> BackendRegistry:
    """Reset and reinitialize default registry (useful for test isolation)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
    return get_default_registry()
