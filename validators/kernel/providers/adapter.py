"""Production-grade provider adapter interface and OpenAI-compatible adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any, Callable

from .errors import (
    AuthenticationError,
    ContextLengthExceededError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)
from .models import (
    Message,
    MessageRole,
    ProviderResponse,
    StreamChunk,
    TokenUsage,
    ToolCallRequest,
    ToolDefinition,
)


class ProviderAdapter(ABC):
    """Abstract interface for LLM provider adapters."""

    def __init__(self, provider_name: str, model_name: str) -> None:
        self.provider_name = provider_name
        self.model_name = model_name

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Execute non-streaming completion."""

    @abstractmethod
    def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        """Stream response chunks from provider."""


class OpenAICompatibleAdapter(ProviderAdapter):
    """Production provider adapter compatible with OpenAI-format endpoints."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o",
        provider_name: str = "openai-compatible",
        default_timeout: float = 30.0,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name, model_name=model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_timeout = default_timeout
        self.transport = transport

    def _prepare_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [m.to_dict() for m in messages],
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [t.to_dict() for t in tools]
            payload["tool_choice"] = "auto"
        payload.update(kwargs)
        return payload

    def _map_http_error(self, status_code: int, error_body: str) -> ProviderError:
        error_msg = error_body
        try:
            parsed = json.loads(error_body)
            if isinstance(parsed, dict) and "error" in parsed:
                err = parsed["error"]
                if isinstance(err, dict):
                    error_msg = err.get("message", error_body)
                elif isinstance(err, str):
                    error_msg = err
        except Exception:
            pass

        lower_msg = error_msg.lower()
        if status_code in (401, 403):
            return AuthenticationError(error_msg, status_code=status_code, provider=self.provider_name)
        if status_code == 429:
            return RateLimitError(error_msg, status_code=status_code, provider=self.provider_name)
        if status_code in (408, 504):
            return TimeoutError(error_msg, status_code=status_code, provider=self.provider_name)
        if status_code == 400 and ("context_length" in lower_msg or "context window" in lower_msg or "maximum context" in lower_msg):
            return ContextLengthExceededError(error_msg, status_code=status_code, provider=self.provider_name)
        return ProviderError(error_msg, status_code=status_code, provider=self.provider_name)

    def _check_cancellation(self, cancellation_token: Any | None) -> None:
        if cancellation_token is None:
            return
        if hasattr(cancellation_token, "is_set") and cancellation_token.is_set():
            raise TimeoutError("Operation was cancelled", provider=self.provider_name)
        if callable(cancellation_token) and cancellation_token():
            raise TimeoutError("Operation was cancelled", provider=self.provider_name)

    def _execute_request(
        self,
        payload: dict[str, Any],
        stream: bool,
        timeout: float | None,
        cancellation_token: Any | None,
    ) -> Any:
        self._check_cancellation(cancellation_token)
        actual_timeout = timeout if timeout is not None else self.default_timeout

        if self.transport is not None:
            try:
                return self.transport(payload, stream, actual_timeout)
            except ProviderError:
                raise
            except Exception as ex:
                raise ProviderError(str(ex), provider=self.provider_name) from ex

        url = f"{self.base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            response = urllib.request.urlopen(req, timeout=actual_timeout)  # noqa: S310
            return response
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", errors="replace")
            raise self._map_http_error(ex.code, body) from ex
        except (urllib.error.URLError, TimeoutError) as ex:
            raise TimeoutError(f"Connection or timeout error: {ex}", provider=self.provider_name) from ex
        except Exception as ex:
            raise ProviderError(f"Unexpected provider transport failure: {ex}", provider=self.provider_name) from ex

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        self._check_cancellation(cancellation_token)
        payload = self._prepare_payload(messages, tools, stream=False, **kwargs)
        raw_result = self._execute_request(payload, stream=False, timeout=timeout, cancellation_token=cancellation_token)

        if isinstance(raw_result, dict):
            data = raw_result
        elif hasattr(raw_result, "read"):
            data = json.loads(raw_result.read().decode("utf-8"))
        else:
            raise ProviderError(f"Invalid response type from transport: {type(raw_result)}", provider=self.provider_name)

        choices = data.get("choices", [])
        if not choices:
            raise ProviderError("Provider returned no choices in response", provider=self.provider_name)

        first_choice = choices[0]
        choice_msg = first_choice.get("message", {})
        content = choice_msg.get("content")
        raw_tool_calls = choice_msg.get("tool_calls", [])

        tool_calls: list[ToolCallRequest] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            call_id = tc.get("id", "")
            fn_name = fn.get("name", "")
            fn_args = fn.get("arguments", {})
            tool_calls.append(ToolCallRequest.from_provider_call(call_id, fn_name, fn_args))

        response_message = Message.assistant(content=content, tool_calls=tool_calls)

        usage_dict = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_dict.get("prompt_tokens", 0),
            completion_tokens=usage_dict.get("completion_tokens", 0),
            total_tokens=usage_dict.get("total_tokens", 0),
        )

        return ProviderResponse(
            message=response_message,
            usage=usage,
            finish_reason=first_choice.get("finish_reason", "stop") or "stop",
            model=data.get("model", self.model_name),
            raw_payload=data,
        )

    def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        self._check_cancellation(cancellation_token)
        payload = self._prepare_payload(messages, tools, stream=True, **kwargs)
        raw_stream = self._execute_request(payload, stream=True, timeout=timeout, cancellation_token=cancellation_token)

        if isinstance(raw_stream, (list, tuple, Iterator)):
            for item in raw_stream:
                self._check_cancellation(cancellation_token)
                if isinstance(item, StreamChunk):
                    yield item
                elif isinstance(item, dict):
                    yield self._parse_stream_dict(item)
            return

        for line in raw_stream:
            self._check_cancellation(cancellation_token)
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                raw_json = line[6:]
                try:
                    chunk_data = json.loads(raw_json)
                    yield self._parse_stream_dict(chunk_data)
                except json.JSONDecodeError:
                    continue

    def _parse_stream_dict(self, data: dict[str, Any]) -> StreamChunk:
        choices = data.get("choices", [])
        finish_reason = None
        delta_content = ""
        delta_tool_calls: list[ToolCallRequest] = []

        if choices:
            first = choices[0]
            finish_reason = first.get("finish_reason")
            delta = first.get("delta", {})
            delta_content = delta.get("content") or ""
            raw_tcs = delta.get("tool_calls", [])
            for tc in raw_tcs:
                fn = tc.get("function", {})
                delta_tool_calls.append(
                    ToolCallRequest.from_provider_call(
                        tc.get("id", ""),
                        fn.get("name", ""),
                        fn.get("arguments", {}),
                    )
                )

        usage = None
        if "usage" in data and data["usage"]:
            u = data["usage"]
            usage = TokenUsage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        return StreamChunk(
            delta_content=delta_content,
            delta_tool_calls=tuple(delta_tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )
