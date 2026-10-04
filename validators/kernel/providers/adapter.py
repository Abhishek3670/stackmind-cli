"""Production-grade provider adapter interface and OpenAI-compatible adapter."""

from __future__ import annotations

import builtins
import json
import os
import re
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any, Callable
from uuid import uuid4

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
        default_timeout: float | None = None,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name, model_name=model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        if default_timeout is None:
            default_timeout = float(os.environ.get("PROVIDER_TIMEOUT", 900.0))
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
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
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

        try:
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
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
            raise TimeoutError(f"Connection or timeout error during stream: {ex}", provider=self.provider_name) from ex
        except Exception as ex:
            raise ProviderError(f"Unexpected stream failure: {ex}", provider=self.provider_name) from ex

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


class OllamaAdapter(ProviderAdapter):
    """Production provider adapter for Ollama-hosted models via native /api/chat."""

    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:7b",
        provider_name: str = "ollama",
        default_timeout: float | None = None,
        options: dict[str, Any] | None = None,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name, model_name=model)
        self.endpoint = endpoint.rstrip("/")
        if default_timeout is None:
            default_timeout = float(os.environ.get("OLLAMA_TIMEOUT", os.environ.get("PROVIDER_TIMEOUT", 900.0)))
        self.default_timeout = default_timeout
        self.options = dict(options) if options else {}
        if "num_gpu" not in self.options and "OLLAMA_NUM_GPU" in os.environ:
            try:
                self.options["num_gpu"] = int(os.environ["OLLAMA_NUM_GPU"])
            except ValueError:
                pass
        self.transport = transport

    def _extract_tool_calls_from_content(self, content: str | None) -> list[ToolCallRequest]:
        if not content or not content.strip():
            return []
        text = content.strip()
        results: list[ToolCallRequest] = []
        try:
            from validators.kernel.providers.gateway import STANDARD_KERNEL_TOOLS
            known_tools = {t.name for t in STANDARD_KERNEL_TOOLS}
        except ImportError:
            known_tools = set()
        known_tools.update({
            "read_file", "write_file", "apply_patch", "move_file", "delete_file",
            "format_file", "list_directory", "inspect_file_lines", "search_code",
            "get_symbol", "run_command", "query_graph", "request_tools",
        })

        # 1. <tool_call> tags
        tags = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
        for tag in tags:
            try:
                data = json.loads(tag.strip())
                if isinstance(data, dict) and data.get("name") in known_tools:
                    args = data.get("arguments", data.get("parameters", {}))
                    results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", data["name"], args))
            except Exception:
                pass
        if results:
            return results

        # 2. Markdown json blocks
        blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        for b in blocks:
            try:
                data = json.loads(b.strip())
                if isinstance(data, dict) and data.get("name") in known_tools:
                    args = data.get("arguments", data.get("parameters", {}))
                    results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", data["name"], args))
            except Exception:
                pass
        if results:
            return results

        # 3. Streaming json parsing
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            try:
                obj, end_idx = decoder.raw_decode(text, idx)
                idx = end_idx
                if isinstance(obj, dict) and obj.get("name") in known_tools:
                    args = obj.get("arguments", obj.get("parameters", {}))
                    results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", obj["name"], args))
            except Exception:
                idx += 1

        return results

    def _prepare_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload_messages = []
        for m in messages:
            msg_dict: dict[str, Any] = {"role": str(m.role)}
            if m.content is not None:
                msg_dict["content"] = m.content
            if m.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in m.tool_calls
                ]
            payload_messages.append(msg_dict)

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": payload_messages,
            "stream": stream,
        }
        if self.options:
            payload["options"] = dict(self.options)
        if tools:
            payload["tools"] = [t.to_dict() for t in tools]
        payload.update(kwargs)
        return payload

    def _execute_request(
        self,
        payload: dict[str, Any],
        stream: bool,
        timeout: float | None,
        cancellation_token: Any | None,
    ) -> Any:
        if cancellation_token is not None:
            if hasattr(cancellation_token, "is_set") and cancellation_token.is_set():
                raise TimeoutError("Operation was cancelled", provider=self.provider_name)
            if callable(cancellation_token) and cancellation_token():
                raise TimeoutError("Operation was cancelled", provider=self.provider_name)

        actual_timeout = timeout if timeout is not None else self.default_timeout

        if self.transport is not None:
            try:
                return self.transport(payload, stream, actual_timeout)
            except ProviderError:
                raise
            except Exception as ex:
                raise ProviderError(str(ex), provider=self.provider_name) from ex

        url = f"{self.endpoint}/api/chat"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            return urllib.request.urlopen(req, timeout=actual_timeout)
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", errors="replace")
            raise ProviderError(
                f"Ollama HTTP error {ex.code}: {body}",
                status_code=ex.code,
                provider=self.provider_name,
            ) from ex
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
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
        payload = self._prepare_payload(messages, tools, stream=False, **kwargs)
        raw_result = self._execute_request(
            payload, stream=False, timeout=timeout, cancellation_token=cancellation_token
        )

        if isinstance(raw_result, dict):
            resp_data = raw_result
        elif hasattr(raw_result, "read"):
            try:
                resp_data = json.loads(raw_result.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
                raise TimeoutError(f"Connection or timeout error reading response: {ex}", provider=self.provider_name) from ex
            except Exception as ex:
                raise ProviderError(f"Failed to read provider response: {ex}", provider=self.provider_name) from ex
        else:
            raise ProviderError(
                f"Invalid response type from transport: {type(raw_result)}",
                provider=self.provider_name,
            )

        msg = resp_data.get("message", {})
        raw_content = msg.get("content", "")
        raw_tool_calls = msg.get("tool_calls", [])

        tool_calls: list[ToolCallRequest] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            call_id = tc.get("id", f"call_{uuid4().hex[:8]}")
            fn_name = fn.get("name", "")
            fn_args = fn.get("arguments", {})
            tool_calls.append(ToolCallRequest.from_provider_call(call_id, fn_name, fn_args))

        if not tool_calls and raw_content:
            tool_calls = self._extract_tool_calls_from_content(raw_content)

        assistant_msg = Message.assistant(content=raw_content, tool_calls=tool_calls)

        prompt_tokens = resp_data.get("prompt_eval_count", 0)
        completion_tokens = resp_data.get("eval_count", 0)
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        return ProviderResponse(
            message=assistant_msg,
            usage=usage,
            finish_reason=resp_data.get("done_reason", "stop") or "stop",
            model=resp_data.get("model", self.model_name),
            raw_payload=resp_data,
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
        payload = self._prepare_payload(messages, tools, stream=True, **kwargs)
        raw_stream = self._execute_request(
            payload, stream=True, timeout=timeout, cancellation_token=cancellation_token
        )

        if isinstance(raw_stream, (list, tuple, Iterator)):
            for item in raw_stream:
                if isinstance(item, StreamChunk):
                    yield item
                elif isinstance(item, dict):
                    yield self._parse_stream_dict(item)
            return

        try:
            for line in raw_stream:
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk_data = json.loads(line)
                    yield self._parse_stream_dict(chunk_data)
                except json.JSONDecodeError:
                    continue
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
            raise TimeoutError(f"Connection or timeout error during stream: {ex}", provider=self.provider_name) from ex
        except Exception as ex:
            raise ProviderError(f"Unexpected stream failure: {ex}", provider=self.provider_name) from ex

    def _parse_stream_dict(self, data: dict[str, Any]) -> StreamChunk:
        msg = data.get("message", {})
        delta_content = msg.get("content") or ""
        raw_tcs = msg.get("tool_calls", [])
        delta_tool_calls: list[ToolCallRequest] = []
        for tc in raw_tcs:
            fn = tc.get("function", {})
            delta_tool_calls.append(
                ToolCallRequest.from_provider_call(
                    tc.get("id", f"call_{uuid4().hex[:8]}"),
                    fn.get("name", ""),
                    fn.get("arguments", {}),
                )
            )
        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            prompt_tok = data.get("prompt_eval_count", 0)
            comp_tok = data.get("eval_count", 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tok,
                completion_tokens=comp_tok,
                total_tokens=prompt_tok + comp_tok,
            )

        return StreamChunk(
            delta_content=delta_content,
            delta_tool_calls=tuple(delta_tool_calls),
            finish_reason=data.get("done_reason") if data.get("done") else None,
            usage=usage,
        )

