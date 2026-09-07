"""StackMind-native provider message, tool call, and response models.

Decouples provider SDK types from the core runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolDefinition:
    """Provider-agnostic specification of a tool callable by the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolCallRequest:
    """A structured function/tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_provider_call(cls, call_id: str, name: str, arguments: str | dict[str, Any]) -> ToolCallRequest:
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                parsed = {"raw": arguments}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        return cls(id=call_id, name=name, arguments=parsed)


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption accounting for an attempt or provider interaction."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.total_tokens == 0 and (self.prompt_tokens or self.completion_tokens):
            object.__setattr__(self, "total_tokens", self.prompt_tokens + self.completion_tokens)

    def __add__(self, other: Any) -> TokenUsage:
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class Message:
    """A single dialogue message in multi-turn conversation."""

    role: MessageRole | str
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.role, MessageRole):
            object.__setattr__(self, "role", self.role.value)
        if isinstance(self.tool_calls, list):
            object.__setattr__(self, "tool_calls", tuple(self.tool_calls))

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=MessageRole.SYSTEM.value, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=MessageRole.USER.value, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        tool_calls: tuple[ToolCallRequest, ...] | list[ToolCallRequest] = (),
    ) -> Message:
        return cls(
            role=MessageRole.ASSISTANT.value,
            content=content,
            tool_calls=tuple(tool_calls),
        )

    @classmethod
    def tool(cls, content: str, tool_call_id: str, name: str | None = None) -> Message:
        return cls(
            role=MessageRole.TOOL.value,
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": str(self.role)}
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data


@dataclass(frozen=True)
class StreamChunk:
    """One streaming increment emitted by a provider adapter."""

    delta_content: str = ""
    delta_tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class ProviderResponse:
    """Authoritative response returned by a provider adapter."""

    message: Message
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    model: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
