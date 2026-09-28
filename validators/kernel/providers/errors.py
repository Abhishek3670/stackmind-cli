"""Standardized error taxonomy for provider gateway and adapters."""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base error for all provider and gateway failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.provider = provider
        self.retryable = retryable
        self.details = details or {}


class RateLimitError(ProviderError):
    """Raised when the provider rate limit or quota has been exceeded."""

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("status_code", 429)
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class TimeoutError(ProviderError):
    """Raised when a provider request or stream times out."""

    def __init__(
        self,
        message: str = "Provider request timed out",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("status_code", 408)
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AuthenticationError(ProviderError):
    """Raised when provider authentication fails (e.g. invalid API key)."""

    def __init__(
        self,
        message: str = "Provider authentication failed",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("status_code", 401)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class ContextLengthExceededError(ProviderError):
    """Raised when prompt and context exceed the provider model's context window."""

    def __init__(
        self,
        message: str = "Model context length exceeded",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("status_code", 400)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class BudgetExceededError(ProviderError):
    """Raised when an attempt exceeds the active contract's max_tokens budget."""

    def __init__(
        self,
        message: str = "Contract token budget exceeded",
        *,
        tokens_used: int = 0,
        max_tokens: int = 0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.tokens_used = tokens_used
        self.max_tokens = max_tokens


class ToolLimitExceededError(ProviderError):
    """Raised when the cumulative tool call limit is exceeded."""

    def __init__(
        self,
        message: str = "Maximum tool call limit exceeded",
        *,
        tool_calls: int = 0,
        max_tool_calls: int = 0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.tool_calls = tool_calls
        self.max_tool_calls = max_tool_calls


class ToolLoopExhaustedError(ProviderError):
    """Raised when the tool loop reaches maximum turns without completing."""

    def __init__(
        self,
        message: str = "Autonomous tool loop reached maximum turns without completion",
        *,
        turns: int = 0,
        max_turns: int = 0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.turns = turns
        self.max_turns = max_turns


class NoProgressLoopError(ProviderError):
    """Raised when a pathological repeated-call loop or cycle is detected."""

    def __init__(
        self,
        message: str = "No progress: repeated tool call pattern detected",
        *,
        pattern: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.pattern = pattern


class ConsecutiveToolFailureError(ProviderError):
    """Raised when multiple consecutive tool calls fail."""

    def __init__(
        self,
        message: str = "Multiple consecutive tool failures detected",
        *,
        failures: int = 0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.failures = failures


class OperationCancelledError(ProviderError):
    """Raised when an operation or tool loop is cancelled via cancellation token."""

    def __init__(
        self,
        message: str = "Operation cancelled",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
