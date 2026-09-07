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
