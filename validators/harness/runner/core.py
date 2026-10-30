"""Composed governed agent runner.

Behaviour lives in focused mixin modules; this module assembles them into the public
``AgentRunner``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

from validators.harness.retrieval import (
    RetrievalPolicy,
    SearchProvider,
    SessionSearchTool,
)

from .commands import _CommandsMixin
from .completion import _CompletionMixin
from .decisions import _DecisionsMixin
from .discovery import _DiscoveryMixin
from .execution import _ExecutionMixin
from .models import (
    EchoLLMProvider,
    HarnessRunResult,
    HarnessTask,
    LLMProvider,
    ResourceReadTracker,
)
from .prompts import _PromptsMixin
from .reconciliation import _ReconciliationMixin
from .staging import _StagingMixin
from .verification import _VerificationMixin
from .workspace import _WorkspaceMixin

class AgentRunner(
    _ExecutionMixin,
    _CompletionMixin,
    _DiscoveryMixin,
    _DecisionsMixin,
    _PromptsMixin,
    _CommandsMixin,
    _StagingMixin,
    _VerificationMixin,
    _ReconciliationMixin,
    _WorkspaceMixin,
):
    """Governed worker execution loop with staged verification."""

    def __init__(
        self,
        project_path: Path,
        agent: str,
        *,
        backend: Any | None = None,
        llm_provider: LLMProvider | None = None,
        provider_adapter: Any | None = None,
        search_provider: SearchProvider | None = None,
        retrieval_policy: RetrievalPolicy | None = None,
        token_budget: int = 1200,
        context_limit: int = 8,
        max_tool_calls: int | None = None,
        max_turns: int | None = None,
        turn_timeout: float | None = None,
        max_lock_retries: int = 3,
        backoff_seconds: float = 0.1,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
        on_token: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, Any], str], None] | None = None,
        on_tool_result: Callable[[str, str, str, Any, Any], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_usage: Callable[[Any], None] | None = None,
        execution_profile: Any | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.sync_path = self.project_path / '.sync'
        self.agent = agent
        self.backend = backend
        self.provider_adapter = provider_adapter
        self.on_usage = on_usage

        # Canonical execution profile & policy resolution
        from validators.kernel.profile import ExecutionProfile, get_profile_policy
        if execution_profile is not None:
            self.execution_profile = ExecutionProfile.from_str(execution_profile)
        else:
            try:
                from validators.kernel.settings import load_settings
                self.execution_profile = ExecutionProfile.from_str(
                    load_settings(self.project_path).execution_profile
                )
            except Exception:
                self.execution_profile = ExecutionProfile.MEDIUM
        self.profile_policy = get_profile_policy(self.execution_profile)
        if backend is not None:
            self.backend_id = getattr(backend, 'backend_id', 'custom-backend')
            self.backend_model = getattr(backend, 'model', None) or 'default'
            if llm_provider is not None:
                self.llm_provider = llm_provider
            elif hasattr(backend, 'complete'):
                self.llm_provider = backend
            else:
                self.llm_provider = EchoLLMProvider()
        else:
            self.llm_provider = llm_provider or EchoLLMProvider()
            self.backend_id = getattr(self.llm_provider, 'provider_name', 'echo')
            self.backend_model = getattr(self.llm_provider, 'model_name', 'stackmind-echo-v1')
        # Native provider adapters use ProviderGateway.  Keep the established
        # LLMProvider protocol intact for deterministic legacy providers.
        if self.provider_adapter is None:
            from validators.kernel.providers.adapter import ProviderAdapter
            if isinstance(self.llm_provider, ProviderAdapter):
                self.provider_adapter = self.llm_provider
        if self.provider_adapter is not None:
            p_name = getattr(self.provider_adapter, 'provider_name', None) or getattr(self.provider_adapter, 'backend_id', None)
            p_model = getattr(self.provider_adapter, 'model', None) or getattr(self.provider_adapter, 'model_name', None)
            if p_name:
                self.backend_id = p_name
            if p_model:
                self.backend_model = p_model
        self.search_tool = SessionSearchTool(search_provider, policy=retrieval_policy)
        self.token_budget = token_budget
        self.context_limit = context_limit
        from validators.kernel.providers.gateway import DEFAULT_MAX_TURNS
        self.max_tool_calls = max_tool_calls
        self.max_turns = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
        # Every provider turn is bounded: the gateway enforces an inactivity
        # timeout per stream and this wall-clock deadline across the tool loop,
        # so a stalled provider can never leave an operation running forever.
        self.turn_timeout = (
            turn_timeout
            if turn_timeout is not None
            else float(os.environ.get("PROVIDER_TURN_TIMEOUT", "1800"))
        )
        self.max_lock_retries = max_lock_retries
        self.backoff_seconds = backoff_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc).astimezone())
        self.sleep_fn = sleep_fn or time.sleep
        self.reader = ResourceReadTracker(max_reads=3)
        self.on_token = on_token
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_thinking = on_thinking
        self._tree_cache: dict[str, Any] | None = None

    def _is_architect_agent(self) -> bool:
        return self.agent in ('claude', 'architecture', 'architect')

    @staticmethod
    def _is_cancelled(cancellation: Event | None) -> bool:
        return cancellation is not None and cancellation.is_set()

    @staticmethod
    def _cancelled_result(task: HarnessTask, operation_id: str | None) -> HarnessRunResult:
        return HarnessRunResult(
            status='cancelled',
            persisted=False,
            task_id=task.identifier,
            reason='operation cancelled',
            meta={'operation_id': operation_id} if operation_id else None,
        )

HarnessRunner = AgentRunner
