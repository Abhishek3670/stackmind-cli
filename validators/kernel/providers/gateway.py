"""Provider gateway mediating model interactions, tool execution, and contract budgets."""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from typing import Any

from validators.kernel.contract import AgentContract
from validators.kernel.session import Attempt
from validators.kernel.tools import ToolGateway

from .adapter import ProviderAdapter
from .errors import BudgetExceededError
from .models import (
    Message,
    MessageRole,
    ProviderResponse,
    TokenUsage,
    ToolCallRequest,
    ToolDefinition,
)

STANDARD_KERNEL_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="read_file",
        description="Read the text content of a file located within the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside workspace"}
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="write_file",
        description="Write text content to a file located within the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside workspace"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
    ),
    ToolDefinition(
        name="run_command",
        description="Execute a sandboxed shell command inside the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command and argument list",
                }
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        name="query_graph",
        description="Query the StackMind derived knowledge graph for symbols or context.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search or query string"}
            },
            "required": ["query"],
        },
    ),
)


class ProviderGateway:
    """Gateway orchestrating provider adapter, tool execution, and contract accounting."""

    def __init__(
        self,
        adapter: ProviderAdapter,
        tool_gateway: ToolGateway,
        *,
        attempt: Attempt | None = None,
        contract: AgentContract | None = None,
        tools: Sequence[ToolDefinition] = STANDARD_KERNEL_TOOLS,
    ) -> None:
        self.adapter = adapter
        self.tool_gateway = tool_gateway
        self.attempt = attempt
        self.contract = contract or (attempt.contract if attempt and hasattr(attempt, "contract") else None)
        self.tools = tuple(tools)
        self.total_usage = TokenUsage()

    def _get_max_tokens(self) -> int | None:
        if self.contract and hasattr(self.contract, "budget") and self.contract.budget:
            return self.contract.budget.get("max_tokens")
        return None

    def _check_budget_before_call(self) -> None:
        max_tokens = self._get_max_tokens()
        if max_tokens is not None and self.total_usage.total_tokens >= max_tokens:
            raise BudgetExceededError(
                f"Contract token budget reached or exceeded: {self.total_usage.total_tokens} >= {max_tokens}",
                tokens_used=self.total_usage.total_tokens,
                max_tokens=max_tokens,
            )

    def _record_and_check_budget(self, usage: TokenUsage) -> None:
        self.total_usage = self.total_usage + usage
        if self.attempt is not None:
            self.attempt.add_usage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        max_tokens = self._get_max_tokens()
        if max_tokens is not None and self.total_usage.total_tokens > max_tokens:
            raise BudgetExceededError(
                f"Contract token budget exceeded: {self.total_usage.total_tokens} > {max_tokens}",
                tokens_used=self.total_usage.total_tokens,
                max_tokens=max_tokens,
            )

    def execute_tool_call(self, tool_call: ToolCallRequest) -> str:
        """Translate and execute a tool call against the ToolGateway."""
        name = tool_call.name
        args = tool_call.arguments or {}

        try:
            if name == "read_file":
                target = args.get("path") or args.get("target") or args.get("filename") or ""
                return self.tool_gateway.read_file(target)

            if name == "write_file":
                target = args.get("path") or args.get("target") or args.get("filename") or ""
                content = args.get("content", args.get("data", args.get("text", "")))
                self.tool_gateway.write_file(target, content)
                return f"Successfully wrote {len(content)} characters to {target}"

            if name == "run_command":
                cmd = args.get("command") or args.get("cmd") or args.get("args")
                if isinstance(cmd, str):
                    cmd = shlex.split(cmd)
                elif not isinstance(cmd, (list, tuple)):
                    return "Error: command must be a list of strings or string"
                result = self.tool_gateway.run_command(cmd)
                return (
                    f"Process finished with returncode {result.returncode}.\n"
                    f"STDOUT: {result.stdout}\n"
                    f"STDERR: {result.stderr}"
                )

            if name == "query_graph":
                query = args.get("query") or args.get("q") or ""
                result = self.tool_gateway.query_graph(query)
                return json.dumps(result) if not isinstance(result, str) else result

            return f"Error: Unknown tool '{name}'"

        except Exception as ex:
            return f"Error executing {name}: {type(ex).__name__}: {ex}"

    def execute_turn(
        self,
        messages: list[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Execute one conversational turn, calling tools if requested."""
        self._check_budget_before_call()

        active_tools = tools if tools is not None else self.tools
        response = self.adapter.complete(
            messages,
            tools=active_tools,
            timeout=timeout,
            cancellation_token=cancellation_token,
            **kwargs,
        )
        self._record_and_check_budget(response.usage)

        messages.append(response.message)

        if response.message.tool_calls:
            for tc in response.message.tool_calls:
                tool_output = self.execute_tool_call(tc)
                messages.append(Message.tool(content=tool_output, tool_call_id=tc.id, name=tc.name))

        return response

    def run_loop(
        self,
        messages: list[Message],
        *,
        max_turns: int = 10,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        """Execute full autonomous reasoning/tool loop until task completion or max_turns."""
        active_tools = tools if tools is not None else self.tools

        for _ in range(max_turns):
            response = self.execute_turn(
                messages,
                tools=active_tools,
                timeout=timeout,
                cancellation_token=cancellation_token,
                **kwargs,
            )
            # If the assistant gave an answer without emitting new tool calls, the turn finished
            if not response.message.tool_calls:
                break

        return messages
