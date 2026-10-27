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
        self._active_stream: Any = None

    def abort(self) -> None:
        """Release an in-flight streaming response so a blocked read returns.

        Called when the gateway's stream watchdog fires or the operation is
        cancelled.  Best-effort by design: it must never raise into the caller
        that is already unwinding a timed-out turn.
        """
        response = self._active_stream
        self._active_stream = None
        if response is None:
            return
        # Closing the HTTPResponse alone can leave a thread blocked inside a
        # read, so shut the socket down first when it is reachable.
        file_obj = getattr(response, "fp", None)
        raw = getattr(file_obj, "raw", None) or file_obj
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

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
            "messages": [
                (lambda d: d.update({"content": "(no output)"}) or d if d.get("role") == "tool" and not str(d.get("content", "")).strip() else d)(m.to_dict())
                for m in messages
            ],
            "stream": stream,
        }
        if stream and ("api.openai.com" in self.base_url or self.provider_name == "openai"):
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
            "User-Agent": "StackMind-CLI/3.3",
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

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("Provider returned no choices in response", provider=self.provider_name)

        first_choice = choices[0] or {}
        choice_msg = first_choice.get("message") or {}
        content = choice_msg.get("content") or ""
        raw_tool_calls = choice_msg.get("tool_calls") or []

        tool_calls: list[ToolCallRequest] = []
        for tc in raw_tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            call_id = tc.get("id") or f"call_{uuid4().hex[:8]}"
            fn_name = fn.get("name") or ""
            fn_args = fn.get("arguments") or {}
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
        self._active_stream = raw_stream

        if isinstance(raw_stream, dict):
            raw_stream = [raw_stream]

        # urllib's HTTPResponse inherits io.IOBase, so a live SSE response
        # satisfies isinstance(x, Iterator) while yielding raw *lines* (bytes)
        # rather than parsed payloads. Taking the pre-parsed branch for such a
        # stream consumed every line and dropped it (neither StreamChunk nor
        # dict matched), so the caller received an empty response. File-like
        # streams must always go through the SSE line parser below.
        is_file_like = hasattr(raw_stream, "read") or hasattr(raw_stream, "readline")

        # OpenAI-compatible endpoints stream a tool call as several fragments:
        # the first carries id/name, later ones carry argument pieces. Buffer
        # them and emit one assembled call when the stream ends, otherwise every
        # fragment becomes its own tool call with an empty name and unparsable
        # arguments.
        pending_tool_calls: dict[int, dict[str, Any]] = {}

        def _flush_tool_calls() -> StreamChunk | None:
            if not pending_tool_calls:
                return None
            return StreamChunk(delta_tool_calls=self._assemble_tool_calls(pending_tool_calls))

        if not is_file_like and isinstance(raw_stream, (list, tuple, Iterator)):
            for item in raw_stream:
                self._check_cancellation(cancellation_token)
                if isinstance(item, StreamChunk):
                    yield item
                elif isinstance(item, dict):
                    yield self._consume_stream_payload(item, pending_tool_calls)
            flushed = _flush_tool_calls()
            if flushed is not None:
                yield flushed
            return

        saw_finish_reason = False
        saw_done_sentinel = False
        try:
            for line in raw_stream:
                self._check_cancellation(cancellation_token)
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line == "data: [DONE]":
                    saw_done_sentinel = True
                    break
                if line.startswith("data: "):
                    raw_json = line[6:].strip()
                    try:
                        chunk_data = json.loads(raw_json)
                        parsed = self._consume_stream_payload(chunk_data, pending_tool_calls)
                        saw_finish_reason = saw_finish_reason or bool(parsed.finish_reason)
                        yield parsed
                    except json.JSONDecodeError:
                        continue
                elif line.startswith("{") and line.endswith("}"):
                    try:
                        chunk_data = json.loads(line)
                        if "error" in chunk_data and isinstance(chunk_data["error"], (dict, str)):
                            err_info = chunk_data["error"]
                            err_msg = err_info.get("message") if isinstance(err_info, dict) else str(err_info)
                            raise ProviderError(f"Proxy error: {err_msg}", provider=self.provider_name)
                        parsed = self._consume_stream_payload(chunk_data, pending_tool_calls)
                        saw_finish_reason = saw_finish_reason or bool(parsed.finish_reason)
                        yield parsed
                    except json.JSONDecodeError:
                        continue
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as ex:
            raise TimeoutError(f"Connection or timeout error during stream: {ex}", provider=self.provider_name) from ex
        except Exception as ex:
            raise ProviderError(f"Unexpected stream failure: {ex}", provider=self.provider_name) from ex

        flushed = _flush_tool_calls()
        if flushed is not None:
            yield flushed
        if saw_done_sentinel and not saw_finish_reason:
            # Some proxies end the stream with [DONE] but omit finish_reason.
            # The sentinel is the server's own terminal signal, so record it
            # rather than letting the caller read this as a truncated stream.
            yield StreamChunk(finish_reason="stop")

    @staticmethod
    def _extract_tool_call_fragments(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the raw tool-call fragments carried by one SSE payload."""
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return []
        container = choices[0].get("delta") or choices[0].get("message") or {}
        if not isinstance(container, dict):
            return []
        raw = container.get("tool_calls")
        if not isinstance(raw, list):
            return []
        return [frag for frag in raw if isinstance(frag, dict)]

    @staticmethod
    def _without_tool_call_fragments(data: dict[str, Any]) -> dict[str, Any]:
        """Shallow-copy a payload with its tool-call fragments removed."""
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return data
        choice = dict(choices[0])
        # Mirror the extraction lookup so we trim the same container we read
        # fragments from, whether it arrived as a delta or a full message.
        container_key = "delta" if choice.get("delta") else "message" if choice.get("message") else None
        if container_key is None:
            return data
        container = choice[container_key]
        if isinstance(container, dict) and container.get("tool_calls"):
            trimmed = dict(container)
            trimmed.pop("tool_calls", None)
            choice[container_key] = trimmed
        out = dict(data)
        out["choices"] = [choice, *choices[1:]]
        return out

    @staticmethod
    def _merge_tool_call_fragments(
        pending: dict[int, dict[str, Any]], fragments: list[dict[str, Any]]
    ) -> None:
        """Fold streamed tool-call fragments into one entry per call index."""
        for fragment in fragments:
            function = fragment.get("function")
            if not isinstance(function, dict):
                function = {}
            index = fragment.get("index")
            if isinstance(index, int):
                entry = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
            elif fragment.get("id") or function.get("name"):
                # Providers that omit ``index`` still signal a new call with
                # an id or a function name.
                entry = pending.setdefault(len(pending), {"id": "", "name": "", "arguments": ""})
            elif pending:
                entry = pending[max(pending)]
            else:
                entry = pending.setdefault(0, {"id": "", "name": "", "arguments": ""})

            if fragment.get("id"):
                entry["id"] = str(fragment["id"])
            if function.get("name"):
                entry["name"] = str(function["name"])

            arguments = function.get("arguments")
            if isinstance(arguments, str):
                entry["arguments"] += arguments
            elif isinstance(arguments, dict):
                entry["arguments"] = json.dumps(arguments)

    @staticmethod
    def _assemble_tool_calls(pending: dict[int, dict[str, Any]]) -> tuple[ToolCallRequest, ...]:
        """Turn the buffered fragments into complete tool calls in index order."""
        assembled: list[ToolCallRequest] = []
        for key in sorted(pending):
            entry = pending[key]
            arguments = str(entry.get("arguments") or "").strip() or "{}"
            assembled.append(
                ToolCallRequest.from_provider_call(
                    entry.get("id") or f"call_{uuid4().hex[:8]}",
                    entry.get("name") or "",
                    arguments,
                )
            )
        return tuple(assembled)

    def _consume_stream_payload(
        self, data: dict[str, Any], pending_tool_calls: dict[int, dict[str, Any]]
    ) -> StreamChunk:
        """Parse one payload, buffering any tool-call fragments it carries."""
        fragments = self._extract_tool_call_fragments(data)
        if fragments:
            self._merge_tool_call_fragments(pending_tool_calls, fragments)
            data = self._without_tool_call_fragments(data)
        return self._parse_stream_dict(data)

    def _parse_stream_dict(self, data: dict[str, Any]) -> StreamChunk:
        choices = data.get("choices") or []
        finish_reason = None
        delta_content = ""
        delta_thinking = ""
        delta_tool_calls: list[ToolCallRequest] = []

        if choices:
            first = choices[0] or {}
            finish_reason = first.get("finish_reason")
            delta = first.get("delta") or first.get("message") or {}
            delta_content = delta.get("content") or ""
            delta_thinking = (
                delta.get("reasoning_content")
                or delta.get("reasoning")
                or delta.get("thinking")
                or data.get("reasoning_summary")
                or ""
            )
            raw_tcs = delta.get("tool_calls") or []
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                delta_tool_calls.append(
                    ToolCallRequest.from_provider_call(
                        tc.get("id") or f"call_{uuid4().hex[:8]}",
                        fn.get("name") or "",
                        fn.get("arguments") or {},
                    )
                )

        usage = None
        if "usage" in data and data["usage"]:
            u = data["usage"]
            usage = TokenUsage(
                prompt_tokens=u.get("prompt_tokens", 0) or 0,
                completion_tokens=u.get("completion_tokens", 0) or 0,
                total_tokens=u.get("total_tokens", 0) or 0,
            )

        return StreamChunk(
            delta_content=delta_content,
            delta_thinking=delta_thinking,
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
        model: str = "ornith-1.5-16k:latest",
        provider_name: str = "ollama",
        default_timeout: float | None = None,
        options: dict[str, Any] | None = None,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        model = (model or "").strip()
        for prefix in ("ollama run ", "ollama ", "ollama/"):
            if model.lower().startswith(prefix):
                model = model[len(prefix):].strip()
        super().__init__(provider_name=provider_name, model_name=model)
        self.endpoint = endpoint.rstrip("/")
        if default_timeout is None:
            default_timeout = float(os.environ.get("OLLAMA_TIMEOUT", os.environ.get("PROVIDER_TIMEOUT", 900.0)))
        self.default_timeout = default_timeout
        self.options = dict(options) if options else {}
        if "num_ctx" not in self.options:
            try:
                self.options["num_ctx"] = int(os.environ.get("OLLAMA_NUM_CTX", 16384))
            except ValueError:
                self.options["num_ctx"] = 16384
        if "num_predict" not in self.options:
            try:
                self.options["num_predict"] = int(os.environ.get("OLLAMA_NUM_PREDICT", 4096))
            except ValueError:
                self.options["num_predict"] = 4096
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

        # 1. <tool_call> tags (JSON or XML)
        tags = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
        for tag in tags:
            tag_str = tag.strip()
            # 1a. Try JSON inside <tool_call>
            try:
                data = json.loads(tag_str)
                if isinstance(data, dict) and data.get("name") in known_tools:
                    args = data.get("arguments", data.get("parameters", {}))
                    results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", data["name"], args))
                    continue
            except Exception:
                pass

            # 1b. Try XML function inside <tool_call>: <function=name><parameter=k>v</parameter></function>
            fn_matches = re.finditer(r"<function=([a-zA-Z0-9_-]+)>(.*?)(?:</function>|$)", tag_str, re.DOTALL)
            for fn_m in fn_matches:
                fn_name = fn_m.group(1)
                fn_body = fn_m.group(2)
                args = {}
                for pm in re.finditer(r"<parameter=([a-zA-Z0-9_-]+)>\s*(.*?)\s*</parameter>", fn_body, re.DOTALL):
                    p_name = pm.group(1)
                    p_val = pm.group(2)
                    try:
                        p_val = json.loads(p_val)
                    except Exception:
                        pass
                    args[p_name] = p_val
                if fn_name in known_tools or not known_tools:
                    results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", fn_name, args))

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

        # 3. Direct XML function outside <tool_call>
        fn_matches = re.finditer(r"<function=([a-zA-Z0-9_-]+)>(.*?)(?:</function>|$)", text, re.DOTALL)
        for fn_m in fn_matches:
            fn_name = fn_m.group(1)
            fn_body = fn_m.group(2)
            args = {}
            for pm in re.finditer(r"<parameter=([a-zA-Z0-9_-]+)>\s*(.*?)\s*</parameter>", fn_body, re.DOTALL):
                p_name = pm.group(1)
                p_val = pm.group(2)
                try:
                    p_val = json.loads(p_val)
                except Exception:
                    pass
                args[p_name] = p_val
            if fn_name in known_tools or not known_tools:
                results.append(ToolCallRequest.from_provider_call(f"call_{uuid4().hex[:8]}", fn_name, args))
        if results:
            return results

        # 4. Streaming json parsing
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

        m_name = (self.model_name or "").strip()
        for prefix in ("ollama run ", "ollama ", "ollama/"):
            if m_name.lower().startswith(prefix):
                m_name = m_name[len(prefix):].strip()
        payload: dict[str, Any] = {
            "model": m_name,
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

        msg = resp_data.get("message") or {}
        raw_content = msg.get("content") or ""
        raw_tool_calls = msg.get("tool_calls") or []

        tool_calls: list[ToolCallRequest] = []
        for tc in raw_tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            call_id = tc.get("id") or f"call_{uuid4().hex[:8]}"
            fn_name = fn.get("name") or ""
            fn_args = fn.get("arguments") or {}
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
        self._active_stream = raw_stream

        # A transport that answers a streaming request with one complete body
        # (dict) must be parsed as a single, terminal payload. Iterating the
        # dict yielded its keys, dropped every line, and produced an empty
        # stream that looked like a successful empty completion.
        if isinstance(raw_stream, dict):
            single = self._parse_stream_dict(raw_stream)
            yield single
            if not single.finish_reason:
                yield StreamChunk(
                    finish_reason=str(raw_stream.get("done_reason") or "stop")
                )
            return

        if isinstance(raw_stream, (list, tuple)):
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
        msg = data.get("message") or {}
        delta_content = msg.get("content") or ""
        delta_thinking = msg.get("thinking") or ""
        raw_tcs = msg.get("tool_calls") or []
        delta_tool_calls: list[ToolCallRequest] = []
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            delta_tool_calls.append(
                ToolCallRequest.from_provider_call(
                    tc.get("id") or f"call_{uuid4().hex[:8]}",
                    fn.get("name") or "",
                    fn.get("arguments") or {},
                )
            )
        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            prompt_tok = data.get("prompt_eval_count", 0) or 0
            comp_tok = data.get("eval_count", 0) or 0
            usage = TokenUsage(
                prompt_tokens=prompt_tok,
                completion_tokens=comp_tok,
                total_tokens=prompt_tok + comp_tok,
            )

        return StreamChunk(
            delta_content=delta_content,
            delta_thinking=delta_thinking,
            delta_tool_calls=tuple(delta_tool_calls),
            finish_reason=data.get("done_reason") if data.get("done") else None,
            usage=usage,
        )


class AnthropicAdapter(ProviderAdapter):
    """Production provider adapter for Anthropic Claude models via Messages API."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        model: str = "claude-3-7-sonnet-20250219",
        provider_name: str = "anthropic",
        default_timeout: float | None = None,
        max_tokens: int = 4096,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name, model_name=model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if default_timeout is None:
            default_timeout = float(os.environ.get("PROVIDER_TIMEOUT", 900.0))
        self.default_timeout = default_timeout
        self.max_tokens = max_tokens
        self.transport = transport

    def _prepare_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system_text = ""
        formatted_messages: list[dict[str, Any]] = []

        for m in messages:
            role_str = m.role.value if hasattr(m.role, "value") else str(m.role)
            if role_str == "system":
                if system_text:
                    system_text += "\n\n" + m.content
                else:
                    system_text = m.content
            elif role_str == "assistant":
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                formatted_messages.append({
                    "role": "assistant",
                    "content": content_blocks if content_blocks else m.content or "",
                })
            elif role_str == "tool":
                formatted_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": getattr(m, "tool_call_id", None) or getattr(m, "name", "tool_result"),
                        "content": m.content,
                    }],
                })
            else:
                formatted_messages.append({
                    "role": "user",
                    "content": m.content,
                })

        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "messages": formatted_messages,
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text

        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        payload.update(kwargs)
        return payload

    def _execute_request(
        self,
        payload: dict[str, Any],
        stream: bool,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
    ) -> Any:
        if self.transport is not None:
            return self.transport(payload, stream, timeout)

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "StackMind-CLI/3.3",
        }
        effective_timeout = timeout if timeout is not None else self.default_timeout

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, headers=headers)
            response = urllib.request.urlopen(req, timeout=effective_timeout)
            if stream:
                return response
            data = json.loads(response.read().decode("utf-8"))
            return data
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            if err.code in (401, 403):
                raise AuthenticationError(f"Anthropic authentication failed: {err_body}", provider=self.provider_name) from err
            elif err.code == 429:
                raise RateLimitError(f"Anthropic rate limit exceeded: {err_body}", provider=self.provider_name) from err
            raise ProviderError(f"Anthropic HTTP error {err.code}: {err_body}", provider=self.provider_name) from err
        except (urllib.error.URLError, TimeoutError, builtins.TimeoutError, socket.timeout) as err:
            raise TimeoutError(f"Anthropic request timed out: {err}", provider=self.provider_name) from err
        except Exception as err:
            raise ProviderError(f"Anthropic request failed: {err}", provider=self.provider_name) from err

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
        raw_resp = self._execute_request(payload, stream=False, timeout=timeout, cancellation_token=cancellation_token)

        content_text = ""
        tool_calls: list[ToolCallRequest] = []
        for block in (raw_resp.get("content") or []):
            if not isinstance(block, dict):
                continue
            b_type = block.get("type")
            if b_type == "text":
                content_text += block.get("text") or ""
            elif b_type == "tool_use":
                tool_calls.append(
                    ToolCallRequest.from_provider_call(
                        block.get("id") or f"call_{uuid4().hex[:8]}",
                        block.get("name") or "",
                        block.get("input") or {},
                    )
                )

        usage = None
        if "usage" in raw_resp:
            u = raw_resp["usage"]
            usage = TokenUsage(
                prompt_tokens=u.get("input_tokens", 0),
                completion_tokens=u.get("output_tokens", 0),
                total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
            )

        resp_msg = Message.assistant(content=content_text, tool_calls=tool_calls)
        return ProviderResponse(
            message=resp_msg,
            usage=usage or TokenUsage(),
            finish_reason=raw_resp.get("stop_reason") or "stop",
            model=raw_resp.get("model", self.model_name),
            raw_payload=raw_resp,
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
        raw_stream = self._execute_request(payload, stream=True, timeout=timeout, cancellation_token=cancellation_token)
        self._active_stream = raw_stream

        if isinstance(raw_stream, (list, tuple, Iterator)):
            for item in raw_stream:
                if isinstance(item, StreamChunk):
                    yield item
            return

        current_tool_id = ""
        current_tool_name = ""
        current_tool_input = ""

        try:
            for line in raw_stream:
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                raw_json = line[6:]
                if raw_json == "[DONE]":
                    break
                try:
                    event = json.loads(raw_json)
                except json.JSONDecodeError:
                    continue

                e_type = event.get("type")
                if e_type == "content_block_start":
                    cb = event.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        current_tool_id = cb.get("id", f"call_{uuid4().hex[:8]}")
                        current_tool_name = cb.get("name", "")
                        current_tool_input = ""
                elif e_type == "content_block_delta":
                    delta = event.get("delta", {})
                    d_type = delta.get("type")
                    if d_type == "text_delta":
                        yield StreamChunk(delta_content=delta.get("text", ""))
                    elif d_type == "thinking_delta":
                        yield StreamChunk(delta_thinking=delta.get("thinking", ""))
                    elif d_type == "input_json_delta":
                        current_tool_input += delta.get("partial_json", "")
                elif e_type == "content_block_stop":
                    if current_tool_name:
                        try:
                            parsed_args = json.loads(current_tool_input) if current_tool_input else {}
                        except Exception:
                            parsed_args = {"raw": current_tool_input}
                        tc = ToolCallRequest.from_provider_call(current_tool_id, current_tool_name, parsed_args)
                        yield StreamChunk(delta_tool_calls=(tc,))
                        current_tool_id = ""
                        current_tool_name = ""
                        current_tool_input = ""
                elif e_type == "message_delta":
                    u = event.get("usage")
                    usage = None
                    if u:
                        usage = TokenUsage(
                            completion_tokens=u.get("output_tokens", 0),
                        )
                    yield StreamChunk(
                        finish_reason=event.get("delta", {}).get("stop_reason"),
                        usage=usage,
                    )
        except Exception as ex:
            raise ProviderError(f"Anthropic stream error: {ex}", provider=self.provider_name) from ex


class GeminiAdapter(ProviderAdapter):
    """Production provider adapter for Google Gemini via generateContent REST API."""

    def __init__(
        self,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        model: str = "gemini-2.0-flash",
        provider_name: str = "gemini",
        default_timeout: float | None = None,
        transport: Callable[[dict[str, Any], bool, float | None], Any] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name, model_name=model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        if default_timeout is None:
            default_timeout = float(os.environ.get("PROVIDER_TIMEOUT", 900.0))
        self.default_timeout = default_timeout
        self.transport = transport

    def _prepare_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_instruction: dict[str, Any] | None = None

        for m in messages:
            role_str = m.role.value if hasattr(m.role, "value") else str(m.role)
            if role_str == "system":
                system_instruction = {"parts": [{"text": m.content}]}
            elif role_str == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif role_str == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": getattr(m, "name", "tool"),
                            "response": {"result": m.content},
                        }
                    }],
                })
            else:
                contents.append({"role": "user", "parts": [{"text": m.content}]})

        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["system_instruction"] = system_instruction

        if tools:
            payload["tools"] = [{
                "function_declarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                    for t in tools
                ]
            }]
        payload.update(kwargs)
        return payload

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        payload = self._prepare_payload(messages, tools, **kwargs)
        url = f"{self.base_url}/models/{self.model_name}:generateContent?key={self.api_key}"
        effective_timeout = timeout if timeout is not None else self.default_timeout

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "StackMind-CLI/3.3"},
            )
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            if err.code in (401, 403):
                raise AuthenticationError(f"Gemini authentication failed: {err_body}", provider=self.provider_name) from err
            elif err.code == 429:
                raise RateLimitError(f"Gemini rate limit exceeded: {err_body}", provider=self.provider_name) from err
            raise ProviderError(f"Gemini HTTP error {err.code}: {err_body}", provider=self.provider_name) from err
        except Exception as err:
            raise ProviderError(f"Gemini request failed: {err}", provider=self.provider_name) from err

        candidates = data.get("candidates") or []
        content_text = ""
        tool_calls: list[ToolCallRequest] = []
        finish_reason = None

        if candidates:
            cand = candidates[0] or {}
            finish_reason = cand.get("finishReason")
            parts = (cand.get("content") or {}).get("parts") or []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                if "text" in p:
                    content_text += p.get("text") or ""
                elif "functionCall" in p:
                    fc = p.get("functionCall") or {}
                    tool_calls.append(
                        ToolCallRequest.from_provider_call(
                            f"call_{uuid4().hex[:8]}",
                            fc.get("name") or "",
                            fc.get("args") or {},
                        )
                    )

        usage = None
        if "usageMetadata" in data:
            um = data["usageMetadata"]
            usage = TokenUsage(
                prompt_tokens=um.get("promptTokenCount", 0),
                completion_tokens=um.get("candidatesTokenCount", 0),
                total_tokens=um.get("totalTokenCount", 0),
            )

        resp_msg = Message.assistant(content=content_text, tool_calls=tool_calls)
        return ProviderResponse(
            message=resp_msg,
            usage=usage or TokenUsage(),
            finish_reason=finish_reason or "stop",
            model=self.model_name,
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
        # For simplicity and robust tool invocation, complete and stream in chunks
        resp = self.complete(messages, tools=tools, timeout=timeout, cancellation_token=cancellation_token, **kwargs)
        if resp.content:
            yield StreamChunk(delta_content=resp.content)
        if resp.tool_calls:
            yield StreamChunk(delta_tool_calls=resp.tool_calls)
        yield StreamChunk(finish_reason=resp.finish_reason, usage=resp.usage)


