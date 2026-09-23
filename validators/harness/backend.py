"""Execution Backend Abstraction Layer for StackMind Phase 7.

Defines the ExecutionBackend protocol, standard adapter interface,
Agent and Model concrete backend implementations, and the BackendRegistry.
Enforces the zero credential leakage invariant across all operations,
RPC responses, and serialized states.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable
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


def _sanitize_string(s: str) -> str:
    """Mask credentials and secrets from text messages and URLs."""
    s = re.sub(r'(?i)(token|password|secret|api[_-]?key|auth\w*)[=:].*?(?=[\s;,&]|$)', '[REDACTED]', s)
    s = re.sub(r'://[^/@\s:]+(:[^/@\s]*)?@', '://[REDACTED]@', s)
    return s


def sanitize_secrets(value: Any) -> Any:
    """Recursively strip or mask credentials from any data structure."""
    if isinstance(value, str):
        return _sanitize_string(value)
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
        self.on_token: Callable[[str], None] | None = None
        self.cancel_event: Any = None

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
    on_token: Callable[[str], None] | None = None
    cancel_event: Any = None

    def __init__(
        self,
        backend_id: str = "ollama",
        model: str = "llama3",
        *,
        endpoint: str | None = "http://localhost:11434",
        credential_ref: str | None = None,
        timeout: float = 300.0,
        budget_policy: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        available: bool = True,
        should_fail: bool = False,
        failure_reason: str = "Simulated model backend failure",
        simulate_timeout: bool = False,
        mock_mode: bool = False,
        default_release_target: str | None = None,
        on_token: Callable[[str], None] | None = None,
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
        self.mock_mode = mock_mode
        self.default_release_target = default_release_target
        self.on_token = on_token
        self.cancel_event = None

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
        if self.status == "not configured" and self.endpoint:
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

        if self.endpoint and not self.mock_mode:
            try:
                req_url = self.endpoint.rstrip("/") + "/api/generate"
                task_title = getattr(task, "title", "").strip()
                task_body = getattr(task, "body", "").strip()
                if not task_title:
                    prompt_text = task_body
                elif not task_body or task_title == task_body:
                    prompt_text = task_title
                else:
                    prompt_text = f"Task: {task_title}\n\n{task_body}"
                data = json.dumps({
                    "model": self.model or "llama3",
                    "prompt": prompt_text,
                    "stream": True,
                }).encode("utf-8")
                req = urllib.request.Request(req_url, data=data, headers={"Content-Type": "application/json", "User-Agent": "StackMind-CLI/3.3"})
                with urllib.request.urlopen(req, timeout=max(self.timeout, 300.0)) as resp:
                    accumulator: list[str] = []
                    token_cb = getattr(request, "on_token", None) or getattr(self, "on_token", None)
                    cancel = getattr(request, "cancellation", None) or getattr(self, "cancel_event", None)
                    parsed_any = False
                    had_lines = False
                    for raw_line in resp:
                        if cancel is not None and cancel.is_set():
                            break
                        line_str = (
                            raw_line.decode("utf-8").strip()
                            if isinstance(raw_line, bytes)
                            else str(raw_line).strip()
                        )
                        if not line_str:
                            continue
                        had_lines = True
                        try:
                            chunk = json.loads(line_str)
                        except json.JSONDecodeError:
                            continue
                        parsed_any = True
                        if isinstance(chunk, dict) and chunk.get("error"):
                            raise BackendExecutionError(f"Ollama error: {chunk['error']}")
                        fragment = chunk.get("response", "")
                        if fragment:
                            accumulator.append(fragment)
                            if token_cb is not None:
                                try:
                                    token_cb(fragment)
                                except Exception:
                                    pass
                        if chunk.get("done"):
                            stats = {}
                            if "eval_count" in chunk:
                                stats["eval_count"] = chunk["eval_count"]
                            if "eval_duration" in chunk:
                                stats["eval_duration"] = chunk["eval_duration"]
                            if stats:
                                payload["_stream_stats"] = stats
                            break
                    if had_lines and not parsed_any:
                        raise BackendExecutionError(
                            f"Model backend '{self.backend_id}' returned an invalid response"
                        )
                    actual_response = "".join(accumulator)
                    if not actual_response.strip():
                        raise BackendExecutionError("Ollama completed without generating a response")
                    payload["summary"] = actual_response.strip()
                    payload["report_markdown"] = (
                        f"# Response from {self.model}\n\n"
                        f"{actual_response.strip()}\n\n"
                        f"Knowledge revision: {getattr(request.context, 'revision', 'unknown')}\n"
                    )
            except (TimeoutError, socket.timeout) as exc:
                raise BackendTimeoutError(
                    f"Model backend '{self.backend_id}' timed out"
                ) from exc
            except urllib.error.HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                err_msg = f"HTTP {exc.code}"
                try:
                    err_json = json.loads(err_body)
                    if isinstance(err_json, dict) and err_json.get("error"):
                        err_msg = f"{err_msg}: {err_json['error']}"
                    elif err_body.strip():
                        err_msg = f"{err_msg}: {err_body.strip()}"
                except Exception:
                    if err_body.strip():
                        err_msg = f"{err_msg}: {err_body.strip()}"
                clean_err_msg = _sanitize_string(err_msg)
                raise BackendExecutionError(
                    f"Ollama {clean_err_msg}"
                ) from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise BackendTimeoutError(
                        f"Model backend '{self.backend_id}' timed out"
                    ) from exc
                endpoint_str = self.endpoint or "local endpoint"
                clean_reason = _sanitize_string(str(exc.reason))
                raise BackendUnavailableError(
                    f"Could not connect to Ollama at {endpoint_str}: {clean_reason}"
                ) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackendExecutionError(
                    f"Model backend '{self.backend_id}' returned an invalid response"
                ) from exc

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
        self._lock = threading.RLock()

    def register(self, backend: ExecutionBackend) -> None:
        if not hasattr(backend, "backend_id") or not backend.backend_id:
            raise ValueError("backend must have a non-empty backend_id")
        with self._lock:
            self._backends[backend.backend_id] = backend

    def unregister(self, backend_id: str) -> None:
        with self._lock:
            self._backends.pop(backend_id, None)

    def get(self, backend_id: str) -> ExecutionBackend:
        with self._lock:
            try:
                return self._backends[backend_id]
            except KeyError as exc:
                raise KeyError(f"Execution backend '{backend_id}' not found") from exc

    def has(self, backend_id: str) -> bool:
        with self._lock:
            return backend_id in self._backends

    def __contains__(self, backend_id: str) -> bool:
        with self._lock:
            return backend_id in self._backends

    def list_backends(self) -> list[dict[str, Any]]:
        """Return public list of registered backends with zero credential exposure."""
        with self._lock:
            backends = list(self._backends.values())
        result = []
        for backend in backends:
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


def discover_ollama_models(endpoint: str = "http://localhost:11434", timeout: float = 1.0) -> list[str]:
    """Discover installed models from a running Ollama endpoint."""
    url = endpoint.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StackMind-CLI/3.3"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])
            return [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]
    except Exception:
        return []


def pick_best_ollama_model(models: list[str]) -> str | None:
    """Pick the most capable coding or chat model from a list of discovered Ollama models."""
    if not models:
        return None

    # Priority 1: Dedicated coding models (e.g. qwen2.5-coder:7b)
    for m in models:
        name_lower = m.lower()
        if "coder" in name_lower or "code" in name_lower:
            return m

    # Priority 2: Modern chat/instruct reasoning models
    preferred_prefixes = ["qwen", "gemma", "llama", "mistral", "deepseek", "phi"]
    for prefix in preferred_prefixes:
        for m in models:
            if m.lower().startswith(prefix):
                return m

    # Priority 3: Non-embedding general model
    for m in models:
        name_lower = m.lower()
        if not any(skip in name_lower for skip in ["bge", "embed", "bert", "rerank"]):
            return m

    return models[0]


_DEFAULT_REGISTRY: BackendRegistry | None = None


def get_default_registry() -> BackendRegistry:
    """Return the global default BackendRegistry, initializing standard adapters."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = BackendRegistry()
        reg.register(AgentExecutionBackend(backend_id="echo-agent", model="stackmind-echo-v1"))
        reg.register(ModelExecutionBackend(backend_id="mock-model", model="mock-llama3", available=True))

        # Auto-detect local Ollama models
        ollama_endpoint = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        discovered_models = discover_ollama_models(ollama_endpoint)
        best_model = pick_best_ollama_model(discovered_models)
        ollama_available = bool(discovered_models)
        selected_model = best_model or "llama3"

        reg.register(
            ModelExecutionBackend(
                backend_id="ollama",
                model=selected_model,
                endpoint=ollama_endpoint,
                available=ollama_available,
            )
        )

        # Register cloud providers if environment variables are present
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            reg.register(
                ModelExecutionBackend(
                    backend_id="anthropic",
                    model="claude-3-7-sonnet",
                    endpoint="https://api.anthropic.com",
                    credential_ref="env:ANTHROPIC_API_KEY",
                    available=True,
                )
            )

        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            reg.register(
                ModelExecutionBackend(
                    backend_id="openai",
                    model="gpt-4o",
                    endpoint="https://api.openai.com",
                    credential_ref="env:OPENAI_API_KEY",
                    available=True,
                )
            )

        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY


def reset_default_registry() -> BackendRegistry:
    """Reset and reinitialize default registry (useful for test isolation)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
    return get_default_registry()
