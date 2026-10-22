"""Regression tests for OpenAI-compatible streaming.

Two defects made a custom provider appear broken while still answering
non-streaming calls:

1. A real ``urllib`` HTTP response inherits ``io.IOBase`` and therefore
   satisfies ``isinstance(x, Iterator)``. The adapter treated it as a
   collection of already-parsed payloads, so every raw SSE line was consumed
   and discarded (neither ``StreamChunk`` nor ``dict``) and callers received
   zero chunks — an agent turn produced no text at all.
2. OpenAI-compatible endpoints emit a tool call as several fragments (id and
   name first, then argument pieces). Each fragment became its own tool call,
   so the gateway dispatched one broken call per fragment instead of one call.
"""

from __future__ import annotations

import io

from validators.kernel.providers.adapter import OpenAICompatibleAdapter
from validators.kernel.providers.models import Message, ToolDefinition

BASE_URL = "http://localhost:20128/v1"


def _adapter(transport) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url=BASE_URL,
        api_key="sk-test",
        model="inception/mercury-2.5",
        provider_name="custom",
        default_timeout=10.0,
        transport=transport,
    )


def _text_tool() -> ToolDefinition:
    return ToolDefinition(
        name="write_file",
        description="Write a file to disk",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    )


def test_stream_parses_sse_from_a_file_like_http_response():
    """A file-like response (like urllib's HTTPResponse) must be line-parsed."""
    # io.BytesIO has the same shape that broke production: file-like *and*
    # registered as collections.abc.Iterator.
    body = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    adapter = _adapter(lambda payload, stream, timeout: io.BytesIO(body))

    chunks = list(adapter.stream([Message.user("hi")]))

    assert chunks, "stream returned no chunks for a file-like SSE response"
    assert "".join(c.delta_content or "" for c in chunks) == "Hello"
    assert [c.finish_reason for c in chunks if c.finish_reason] == ["stop"]


def test_stream_merges_a_fragmented_tool_call_into_one_call():
    """Argument fragments reassemble into a single tool call with parsed args."""
    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"pa'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": None,
                             "function": {"name": None, "arguments": 'th": "a.py", '}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": None,
                             "function": {"name": None, "arguments": '"content": "x=1"}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    adapter = _adapter(lambda payload, stream, timeout: payloads)

    chunks = list(adapter.stream([Message.user("write it")], tools=[_text_tool()]))

    tool_calls = [t for c in chunks for t in c.delta_tool_calls]
    assert len(tool_calls) == 1, tool_calls
    call = tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "write_file"
    assert call.arguments == {"path": "a.py", "content": "x=1"}
    # Fragments must not leak out before the call is complete.
    assert [c.finish_reason for c in chunks if c.finish_reason] == ["tool_calls"]


def test_stream_keeps_parallel_fragmented_tool_calls_separate():
    """Interleaved calls (index 0 and 1) must not be merged together."""
    def frag(index, call_id, name, args):
        return {
            "choices": [
                {"delta": {"tool_calls": [
                    {"index": index, "id": call_id,
                     "function": {"name": name, "arguments": args}}
                ]}}
            ]
        }

    payloads = [
        frag(0, "call_0", "write_file", '{"pa'),
        frag(1, "call_1", "write_file", '{"pa'),
        frag(0, None, None, 'th": "first.py"}'),
        frag(1, None, None, 'th": "second.py"}'),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    adapter = _adapter(lambda payload, stream, timeout: payloads)

    chunks = list(adapter.stream([Message.user("write both")], tools=[_text_tool()]))

    tool_calls = [t for c in chunks for t in c.delta_tool_calls]
    assert len(tool_calls) == 2, tool_calls
    assert {t.id for t in tool_calls} == {"call_0", "call_1"}
    by_id = {t.id: t.arguments for t in tool_calls}
    assert by_id["call_0"] == {"path": "first.py"}
    assert by_id["call_1"] == {"path": "second.py"}


def test_stream_without_tool_calls_emits_no_tool_chunk():
    """A plain text stream must not gain an extra synthetic chunk."""
    payloads = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    adapter = _adapter(lambda payload, stream, timeout: payloads)

    chunks = list(adapter.stream([Message.user("hi")]))

    assert len(chunks) == 2
    assert all(not c.delta_tool_calls for c in chunks)


def test_complete_still_works_for_a_file_like_response():
    """The non-streaming path shares the response type, so keep it covered."""
    body = (
        b'{"choices":[{"message":{"role":"assistant","content":"plain"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}'
    )
    adapter = _adapter(lambda payload, stream, timeout: io.BytesIO(body))

    response = adapter.complete([Message.user("hi")])

    assert response.message.content == "plain"
    assert response.usage.total_tokens == 3
